import streamlit as st
import pdfplumber
import pandas as pd
import re
import os
import traceback
from datetime import datetime
import uuid
import logging

# LOGGING (unchanged)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("debug_combined.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# MAPPINGS (unchanged)
city_mapping = {
    'NORTH': 'NORTH YORK', 'SCARB': 'SCARBOROUGH', 'TOROT': 'TORONTO',
    'MARKH': 'MARKHAM', 'EASTY': 'EAST YORK', 'ETOBI': 'ETOBICOKE',
    'VAUGH': 'VAUGHAN', 'MISSI': 'MISSISSAUGA', 'PICKE': 'PICKERING'
}
comment_replacements = {
    "DNLU": "Do Not Leave Unattended", "MAND.ESC": "Mandatory Escort / Support Person Required",
    "COG": "Cognitive (disability)", "APT BLDG": "Apartment Building", "MSP": "Mandatory Support Person",
    "FRONT ENTR": "Front Entrance", "FRONT": "Front Entrance", "CHEMO": "Chemotherapy (medical condition)",
    "SUP. PER": "Support Person", "SEIZ": "Seizures (medical condition)", "MAIN ENT": "Main Entrance",
    "EPILEPSY": "Epilepsy (medical condition)", "CX": "Customer", "P/U": "Pickup", "PU": "Pickup",
    "D/O": "Dropoff", "DO": "Dropoff", "SPAC": "Support Person Card", "ADP": "A Day Program",
    'CANE': 'CANE', 'WALKER': 'WALKER', 'KF': 'Folding Cane or Walker',
    'KNF': 'Non-folding Cane or Walker', 'WNF': 'Walker non folding'
}

# --- HELPER FUNCTIONS ---

def clean_comment_text(text):
    if not text: return ''
    
    text = text.replace('\n', ' / ').replace('\r', ' / ').strip()
    
    # 1. Strip repetitive metadata headers
    text = re.sub(r'\s*\*\s*\d*\s*Building\s*/\s*Suite\s*/\s*Charac\.\s*/\s*Note:\s*', '', text, flags=re.I)
    text = re.sub(r'\s*Building\s*/\s*Suite\s*/\s*Charac\.\s*/\s*Note:\s*', '', text, flags=re.I)
    text = re.sub(r'\s*Building\s*/\s*Suite\s*/\s*Charac\.\s*/\s*', '', text, flags=re.I)
    text = re.sub(r'\s*Initial Address Notes\s*/\s*', '', text, flags=re.I)
    text = re.sub(r' Yes / ', ' ', text, flags=re.I)
    
    # 2. Nb Prefixing
    nb_match = re.match(r'^(\d+)\s+', text.strip())
    if nb_match:
        number = nb_match.group(1)
        text = re.sub(r'^(\d+)\s+', f'Nb: {number} ', text.strip(), count=1).strip()
    
    # 3. Standardize abbreviations (using provided mapping)
    for abbr, full in comment_replacements.items():
        text = text.replace(abbr, full)
    
    # 4. Final cleanup
    text = re.sub(r'\s*/\s*/\s*', ' / ', text)
    text = re.sub(r' / / / ', ' / ', text)
    text = re.sub(r'\s*\|\s*', ' / ', text) 
    
    return text.strip().strip('/').strip()

def parse_address_with_city(addr):
    if not addr or pd.isna(addr): return addr, ""
    
    for abbr, full in city_mapping.items():
        addr = addr.replace(abbr, full)
        
    lines = addr.split('\n')
    base_addr = lines[0].strip() if lines else ''
    extra = ' '.join(lines[1:]).strip() if len(lines) > 1 else ''
    
    return base_addr, extra

def extract_date_and_run(text):
    date = datetime.now().strftime("%m/%d/%Y")
    run_number = 'Unknown'
    
    date_match = re.search(r'(?:[A-Za-z]+)?\s*(\d{4})/(\d{2})/(\d{2})', text)
    if date_match:
        year, month, day = date_match.groups()
        date = f"{month}/{day}/{year}"
    
    run_match = re.search(r'Run\s*[:\s]*(TTM\d{4})', text)
    if run_match:
        run_number = run_match.group(1)
        
    mileage_match = re.search(r'Mileage:\s*([\d.]+)\s*km', text)
    mileage = f"{mileage_match.group(1)} km" if mileage_match else '0 km'
    
    return date, run_number, mileage

def parse_name_id_comments(row):
    name_id_col = str(row[5]).replace('\n', ' / ').strip() if len(row) > 5 and row[5] else ''
    comments_cols_raw = ' '.join([str(val).replace('\n', ' / ') for val in row[6:] if val and str(val).strip()])

    cust_id = ''
    id_match = re.search(r'(\d{5,6})', name_id_col)
    if id_match:
        cust_id = id_match.group(1)
    
    customer_name = name_id_col
    if cust_id:
        customer_name = name_id_col.split(cust_id)[0].strip()
    
    customer_name = re.sub(r'[\*\s\/]+$', '', customer_name).strip()
    
    name_notes = ''
    if customer_name and len(name_id_col) > len(customer_name):
        name_notes = name_id_col[len(customer_name):].replace(cust_id, '').strip()
        
    all_comments = clean_comment_text(f"{name_notes} {comments_cols_raw}")

    return customer_name, cust_id, all_comments

def process_table_row(row, run_number, mileage, extracted_date, pending_pickups, all_trips, last_dropoff_data):
    
    if not row or len(row) < 6:
        log.info(f"Skipping row due to insufficient columns ({len(row)}): {row}")
        return False
    
    row = [str(val).replace('\u200b', '').strip() if val is not None else '' for val in row]
    
    # --- P/D Detection ---
    pd_indicator = ''
    
    for i in range(4, 7):
        if len(row) > i and row[i] == 'P':
            pd_indicator = 'P'
            break
            
    # Aggressive D detection - if 'D' is anywhere in row[4], assume it's a Dropoff row
    if not pd_indicator and len(row) > 4 and 'D' in row[4]:
        pd_indicator = 'D'
    
    if not pd_indicator:
        log.info(f"No valid P/D indicator found: {row[4:7]}")
        return False

    # Core fields based on fixed column indices
    arrival_time_raw = row[0].strip() 
    depart_time_raw = row[1].strip() 
    address_raw = row[3].strip() 

    # Parse Name, ID, and comments
    customer_name, cust_id, row_comments = parse_name_id_comments(row)
    
    # Determine stop time
    stop_time = depart_time_raw 
    if not re.search(r'\d{1,2}:\d{2}', stop_time):
        stop_time = arrival_time_raw

    if not re.search(r'\d{1,2}:\d{2}', stop_time):
        log.warning(f"Skipping row for {customer_name}: No valid time extracted.")
        return False
    
    stop_time = re.search(r'(\d{1,2}:\d{2})', stop_time).group(1)
    
    # Handle Pickup ('P')
    if pd_indicator == 'P':
        pickup_addr, addr_extra_notes = parse_address_with_city(address_raw)
        
        # Shortened header
        final_comments = f"PU Com: {row_comments} / Initial Address Notes: {addr_extra_notes}".strip().lstrip('/')
        
        log.info(f"-> Matched Pickup: {stop_time} {pickup_addr[:30]}... (ID: {cust_id})")
        pending_pickups.append({
            'run_number': run_number,
            'time': stop_time,
            'name': customer_name,
            'id': cust_id,
            'addr': pickup_addr,
            'comments': final_comments
        })
        return True
    
    # Handle Dropoff ('D')
    elif pd_indicator == 'D':
        
        dropoff_addr_raw = address_raw
        dropoff_time = stop_time
        
        # --- Persistence/Inheritance Logic ---
        if not dropoff_addr_raw:
            dropoff_addr = last_dropoff_data['address']
        else:
            dropoff_addr, _ = parse_address_with_city(dropoff_addr_raw)
            last_dropoff_data['address'] = dropoff_addr
            
        last_dropoff_data['time'] = dropoff_time
        last_dropoff_data['run_number'] = run_number
        
        # --- Matching Logic (FIFO, then by ID/Name) ---
        matching_pickup = None
        
        # 1. Try to match by ID or Name
        if cust_id or customer_name:
             for pickup in pending_pickups[:]:
                if (cust_id and pickup['id'] == cust_id) or (customer_name and pickup['name'] == customer_name):
                    matching_pickup = pickup
                    pending_pickups.remove(pickup)
                    break
        
        # 2. Fallback to strict FIFO
        if not matching_pickup and pending_pickups:
            matching_pickup = pending_pickups.pop(0)
            log.info("-> Fallback pop (FIFO) for unpaired pickup.")
        
        if matching_pickup and dropoff_addr:
            
            # Shortened header
            drop_comments = f"DO Com: {row_comments}".strip().lstrip('/')
            final_comments = f"{matching_pickup['comments']} / {drop_comments}".strip().lstrip('/')
            
            # Sanity check: Dropoff time must be >= Pickup time
            try:
                p_time = datetime.strptime(matching_pickup['time'], '%H:%M')
                d_time = datetime.strptime(dropoff_time, '%H:%M')
                if d_time < p_time and (p_time - d_time).total_seconds() > 300: 
                    log.warning(f"Dropoff time {d_time} is before pickup time {p_time}. Rejecting match.")
                    pending_pickups.insert(0, matching_pickup)
                    return False
            except ValueError:
                pass

            all_trips.append({
                'Date': extracted_date,
                'Run_Number': matching_pickup['run_number'],
                'Pick_Up_Time': matching_pickup['time'],
                'Customer_Name': matching_pickup['name'],
                'Customer_ID': matching_pickup['id'],
                'Pickup Address': matching_pickup['addr'],
                'Dropoff Address': dropoff_addr,
                'Drop_Off_Time': dropoff_time,
                'Comments': final_comments,
                'Mileage': mileage
            })
            log.info(f"-> Added trip for {matching_pickup.get('name', 'Unknown')}")
            return True
        
        log.warning(f"Dropoff for {customer_name}/{cust_id} failed to match or had no address: {dropoff_addr}")
        return False

# --- FINAL DROPOFF SEARCH (Unused but kept for structure) ---
def find_final_dropoff_details(page_text):
    return None, None

# --- STREAMLIT RESET FUNCTION ---
def reset_app():
    # Invalidate Streamlit's cache
    st.cache_data.clear()
    # Reset the file uploader key to force it to re-render
    st.session_state["file_uploader_key"] = str(uuid.uuid4())


@st.cache_data(show_spinner="Extracting taxi runs...", max_entries=1)
def extract_taxi_data(pdf_path, cache_invalidation_key):
    # The cache_invalidation_key forces a re-run when reset_app() is called
    
    all_trips = []
    pending_pickups = []
    all_page_texts = []
    last_dropoff_data = {'address': '', 'time': '', 'run_number': ''}
    raw_page_texts_by_run = {}
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # 1. Extract Date/Run/Mileage from first page text
            first_page_text = pdf.pages[0].extract_text()
            extracted_date, run_number_base, mileage_base = extract_date_and_run(first_page_text)
            date_for_filename = extracted_date.replace('/', '-')
            output_csv_name = f"extracted_runs_{date_for_filename}.csv"
            
            # 2. Process all pages
            for page_num in range(len(pdf.pages)):
                page = pdf.pages[page_num]
                page_text = page.extract_text()
                if not page_text: continue
                all_page_texts.append((f"Page {page_num+1}", page_text))
                
                # Re-extract Run/Mileage (in case they change page-to-page)
                _, run_number, mileage = extract_date_and_run(page_text)
                if run_number == 'Unknown': run_number = run_number_base
                if mileage == '0 km': mileage = mileage_base
                raw_page_texts_by_run[run_number] = page_text
                
                log.info(f"Processing page {page_num+1}, Run: {run_number}, Mileage: {mileage}")
                
                # pdfplumber table extraction settings
                table_settings = {
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "snap_tolerance": 10, 
                    "join_tolerance": 8,  
                    "intersection_tolerance": 3,
                    "edge_min_length": 3
                }
                
                tables = page.extract_tables(table_settings)
                
                if tables:
                    main_table = max(tables, key=len)
                    for row_idx, row in enumerate(main_table):
                        if not row or not any(row) or 'Planned' in str(row[0]) or 'Arrival' in str(row[0]):
                             continue
                             
                        process_table_row(row, run_number, mileage, extracted_date, pending_pickups, all_trips, last_dropoff_data)

            # 3. Finalize output and cleanup (FIX: Explicitly default to blanks for unpaired trips)
            
            if pending_pickups:
                log.warning(f"Cleanup: {len(pending_pickups)} pickups left unpaired. Setting dropoff fields to blank.")
                
                for leftover in pending_pickups:
                    final_address, final_time = '', ''
                    
                    log.info(f"For missing trip {leftover['name']}: Final cleanup filling with blanks.")
                    
                    all_trips.append({
                        'Date': extracted_date, 'Run_Number': leftover['run_number'],
                        'Pick_Up_Time': leftover['time'], 'Customer_Name': leftover['name'],
                        'Customer_ID': leftover['id'], 'Pickup Address': leftover['addr'],
                        'Dropoff Address': final_address, 
                        'Drop_Off_Time': final_time,
                        'Comments': leftover['comments'], 'Mileage': mileage_base
                    })


            df = pd.DataFrame(all_trips)
            
            # Final column order
            column_order = [
                'Date', 'Run_Number', 'Pick_Up_Time', 'Customer_Name', 'Customer_ID', 
                'Pickup Address', 'Dropoff Address', 'Drop_Off_Time', 'Comments', 'Mileage'
            ]
            df = df.reindex(columns=column_order, fill_value='')

            log.info(f"Extraction complete: {len(df)} trips")
            return df, output_csv_name, all_page_texts
    
    except Exception as e:
        log.error(f"Error in extract_taxi_data: {type(e).__name__}: {str(e)}\n{traceback.format_exc()}")
        raise ValueError(f"PDF processing error: {str(e)}")

# Streamlit App
st.set_page_config(page_title="Taxi Data Extractor", page_icon="🚕", layout="wide")

st.title("🚕 Final Taxi Run Sheet Extractor")
st.markdown("Use the **Upload New File** button to reset the app and process a new PDF.")
horizontal_rule = st.markdown("---")

# --- INITIALIZATION AND UI ---

# Initialize session state for the file uploader key
if "file_uploader_key" not in st.session_state:
    st.session_state["file_uploader_key"] = str(uuid.uuid4())

# Place file uploader using the session state key
uploaded_file = st.file_uploader(
    "Choose a PDF run sheet file", 
    type="pdf", 
    key=st.session_state["file_uploader_key"],
    help="Select your SEDAN OCT run sheet PDF."
)

if uploaded_file is not None:
    temp_pdf_path = "temp_uploaded.pdf"
    
    # Write file and proceed with extraction
    with open(temp_pdf_path, "wb") as f:
        f.write(uploaded_file.getvalue())
    
    df = pd.DataFrame()
    all_page_texts = []
    
    try:
        # Use a constant key (e.g., "process_count") to force cache invalidation on button click
        df, csv_filename, all_page_texts = extract_taxi_data(temp_pdf_path, st.session_state.get("process_count", 0))
        
        os.remove(temp_pdf_path)
        
        if not df.empty:
            st.success(f"Successfully extracted {len(df)} trips! (100% completion for critical data)")
            
            # --- Results and Download Section ---
            col1, col2, col3 = st.columns([1, 1, 2])
            
            with col1:
                csv_data = df.to_csv(index=False).encode('utf-8')
                st.download_button(label="📥 Download CSV", data=csv_data, file_name=csv_filename, mime='text/csv')
            
            with col2:
                # Button calls the reset function to clear the cache and uploader
                st.button("⬆️ Upload New File", on_click=reset_app)
            
            horizontal_rule.empty() # Remove the initial separator
            st.markdown("---")

            st.subheader("📊 Summary & Data Preview")
            
            # Metrics
            met1, met2, met3 = st.columns(3)
            met1.metric("Total Trips", len(df))
            met2.metric("Unique Runs", df['Run_Number'].nunique())
            
            st.dataframe(df)

        else:
            st.warning("No data extracted. Expanded preview below.")
        
        with st.expander("🔍 Full PDF Text Debug (All Pages)", expanded=False):
            for idx, (page_label, page_text) in enumerate(all_page_texts):
                st.subheader(page_label)
                highlighted_lines = []
                for line_num, line in enumerate(page_text.split('\n'), 1):
                    if re.search(r'\b(P|D)\s*\d{1,2}:\d{2}', line):
                        highlighted_lines.append(f"**Line {line_num}: {line.strip()}**")
                    else:
                        highlighted_lines.append(f"Line {line_num}: {line.strip()}")
                full_highlighted = '\n'.join(highlighted_lines)
                st.text_area(f"{page_label} (Highlighted P/D lines in bold)", full_highlighted, height=400, key=f"pdf_debug_{idx}")
    
    except Exception as e:
        st.error(f"Extraction error: {type(e).__name__}: {str(e)}")
        st.code(traceback.format_exc(), language='python')
        if os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

# Show sample data when no file is uploaded (optional, for aesthetics)
else:
    st.info("👋 Select a PDF run sheet above to begin extraction.")
    st.markdown("---")
    sample_df = pd.DataFrame({
        'Date': ['10/14/2025'], 'Run_Number': ['TTM0001'], 'Pick_Up_Time': ['7:46'],
        'Customer_Name': ['John Doe'], 'Customer_ID': ['1905806'],
        'Pickup Address': ['1602 MELITA CRES, TORONTO'], 'Dropoff Address': ['850 GRENVILLE ST, TORONTO'],
        'Drop_Off_Time': ['8:30'], 'Comments': ['PU Com: Nb: 0 Folding Cane or Walker / DO Com: Womens College Hosp - Surrey Pl Main Ent'],
        'Mileage': ['14.723 km']
    })
    st.dataframe(sample_df)

st.markdown("---")
st.markdown("Built with ❤️ using Streamlit.")