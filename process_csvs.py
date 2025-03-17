import os
import glob
import logging
from extract_book_data import process_books

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def process_all_csvs():
    """Process all CSV files in the data/raw_csv directory."""
    # Create output directory if it doesn't exist
    os.makedirs("data/processed", exist_ok=True)
    
    # Get all CSV files in the raw_csv directory
    csv_files = glob.glob("data/raw_csv/*.csv")
    total_files = len(csv_files)
    
    logger.info(f"Found {total_files} CSV files to process")
    
    for i, input_file in enumerate(csv_files, 1):
        try:
            # Create output filename
            filename = os.path.basename(input_file)
            output_file = os.path.join("data/processed", f"processed_{filename}")
            
            logger.info(f"Processing file {i}/{total_files}: {filename}")
            process_books(input_file, output_file, batch_size=5)
            
        except Exception as e:
            logger.error(f"Error processing {filename}: {e}")
            continue

if __name__ == "__main__":
    process_all_csvs()