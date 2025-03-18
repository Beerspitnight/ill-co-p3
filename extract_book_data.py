import os
import time
import logging
import pandas as pd
import requests
from typing import Tuple, Optional
from tenacity import retry, stop_after_attempt, wait_exponential
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv
import glob

# Load environment variables
load_dotenv()

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Rate limits
GOOGLE_BOOKS_CALLS = 100
OPEN_LIBRARY_CALLS = 60
PERIOD = 60  # 1 minute

# Get API key from environment
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY")
if not GOOGLE_BOOKS_API_KEY:
    raise RuntimeError("GOOGLE_BOOKS_API_KEY environment variable is not set")

@sleep_and_retry
@limits(calls=GOOGLE_BOOKS_CALLS, period=PERIOD)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_google_books_summary(title: str, author: str) -> Optional[str]:
    """
    Fetch book summaries from Google Books API with rate limiting and retries.
    
    Args:
        title (str): Book title
        author (str): Book author
        
    Returns:
        Optional[str]: Book description or None if not found
    """
    query = f"{title} {author}".replace(" ", "+")
    url = f"https://www.googleapis.com/books/v1/volumes?q={query}&key={GOOGLE_BOOKS_API_KEY}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        for item in data.get("items", []):
            volume_info = item.get("volumeInfo", {})
            if description := volume_info.get("description"):
                logger.info(f"Found Google Books summary for: {title}")
                return description
        
        logger.warning(f"No Google Books summary found for: {title}")
        return None

    except requests.RequestException as e:
        logger.error(f"Error fetching Google Books data for {title}: {e}")
        return None

@sleep_and_retry
@limits(calls=OPEN_LIBRARY_CALLS, period=PERIOD)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_open_library_details(title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch book details from Open Library with rate limiting and retries.
    
    Args:
        title (str): Book title
        
    Returns:
        Tuple[Optional[str], Optional[str]]: (Table of contents, Full text link)
    """
    query = title.replace(" ", "+")
    url = f"https://openlibrary.org/search.json?title={query}&limit=1"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if docs := data.get("docs", []):
            book_data = docs[0]
            first_sentence = book_data.get("first_sentence", [""])[0] if "first_sentence" in book_data else None
            key = book_data.get("key")
            full_text_link = f"https://openlibrary.org{key}" if key else None
            
            logger.info(f"Found Open Library details for: {title}")
            return first_sentence, full_text_link

        logger.warning(f"No Open Library details found for: {title}")
        return None, None

    except requests.RequestException as e:
        logger.error(f"Error fetching Open Library data for {title}: {e}")
        return None, None

def process_books(input_csv: str, output_csv: str, batch_size: int = 10):
    try:
        # Read CSV in chunks
        for chunk in pd.read_csv(input_csv, chunksize=batch_size):
            # Process this chunk
            for index, row in chunk.iterrows():
                title = row["title"]
                author = row["authors"]

                google_summary = fetch_google_books_summary(title, author)
                toc, full_text = fetch_open_library_details(title)

                chunk.at[index, "Summary"] = google_summary
                chunk.at[index, "Table of Contents"] = toc
                chunk.at[index, "Full Text Link"] = full_text

            # Write this chunk to CSV
            if not os.path.exists(output_csv):
                # First chunk - write with headers
                chunk.to_csv(output_csv, index=False, mode='w')
            else:
                # Append without headers
                chunk.to_csv(output_csv, index=False, mode='a', header=False)
            
            logger.info(f"Processed and saved batch of {len(chunk)} books")

    except Exception as e:
        logger.error(f"Error processing books: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    # Create output directory if it doesn't exist
    os.makedirs("data/processed", exist_ok=True)
    
    # Get all CSV files in the raw_csv directory
    csv_files = glob.glob("data/raw_csv/*.csv")
    
    if not csv_files:
        logger.error("No CSV files found in data/raw_csv directory")
        exit(1)
        
    logger.info(f"Found {len(csv_files)} CSV files to process")
    
    for input_file in csv_files:
        try:
            # Create output filename
            filename = os.path.basename(input_file)
            output_file = os.path.join("data/processed", f"processed_{filename}")
            
            logger.info(f"Processing file: {filename}")
            process_books(input_file, output_file)
            
        except Exception as e:
            logger.error(f"Error processing {filename}: {e}")
            continue
