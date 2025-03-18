import requests
import sqlite3
import os
import logging
from typing import Optional, Dict, Any, Tuple
from ratelimit import limits, sleep_and_retry
from tenacity import retry, stop_after_attempt, wait_exponential
from dotenv import load_dotenv
from contextlib import contextmanager
import glob
import pandas as pd
import time

# Load environment variables and setup logging
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class Config:
    """Configuration settings."""
    OPEN_LIBRARY_TEXT_URL = "https://archive.org/stream/{identifier}/text.txt"
    OPEN_LIBRARY_COVER_URL = "https://covers.openlibrary.org/b/olid/{olid}-L.jpg"
    OPEN_LIBRARY_API_URL = "https://openlibrary.org"
    OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
    DB_PATH = "books.db"
    BATCH_SIZE = 10
    API_TIMEOUT = 10
    API_CALLS_PER_MINUTE = 60
    INITIAL_BACKOFF = 2  # seconds
    MAX_BACKOFF = 30  # seconds
    MAX_RETRIES = 5
    MIN_WAIT_BETWEEN_REQUESTS = 2  # seconds

def init_database(conn: sqlite3.Connection):
    """Initialize database tables."""
    cursor = conn.cursor()
    try:
        # Create books table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            olid TEXT UNIQUE,
            title TEXT,
            authors TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Create full_texts table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS full_texts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER,
            text_content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books (id)
        );
        """)

        # Create images table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER,
            image_type TEXT,
            image_data BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books (id)
        );
        """)

        conn.commit()
        logger.info("Database initialized successfully")

        # Verify tables were created
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        logger.info(f"Available tables: {[table[0] for table in tables]}")
        
    except sqlite3.Error as e:
        logger.error(f"Database initialization failed: {e}")
        raise

@contextmanager
def get_db_connection():
    """Database connection context manager."""
    conn = None
    try:
        conn = sqlite3.connect(Config.DB_PATH)
        yield conn
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        raise
    finally:
        if conn:
            conn.close()

@sleep_and_retry
@limits(calls=Config.API_CALLS_PER_MINUTE, period=60)
@retry(
    stop=stop_after_attempt(Config.MAX_RETRIES),
    wait=wait_exponential(multiplier=Config.INITIAL_BACKOFF, min=Config.INITIAL_BACKOFF, max=Config.MAX_BACKOFF),
    reraise=True
)
def make_api_request(url: str, params: Optional[Dict] = None) -> Optional[requests.Response]:
    """Make rate-limited API request with retries and exponential backoff."""
    try:
        response = requests.get(url, params=params, timeout=Config.API_TIMEOUT)
        
        # Handle different error codes
        if response.status_code == 429:  # Too Many Requests
            logger.warning("Rate limit hit, backing off...")
            time.sleep(Config.MAX_BACKOFF)
            raise requests.exceptions.RequestException("Rate limit exceeded")
        elif response.status_code >= 500:  # Server errors
            logger.warning(f"Server error {response.status_code}, retrying...")
            time.sleep(Config.INITIAL_BACKOFF)
            raise requests.exceptions.RequestException(f"Server error: {response.status_code}")
            
        response.raise_for_status()
        time.sleep(Config.MIN_WAIT_BETWEEN_REQUESTS)  # Ensure minimum wait between requests
        return response
        
    except requests.RequestException as e:
        logger.error(f"API request failed: {e}")
        return None

def validate_olid(olid: str) -> bool:
    """Validate Open Library ID format."""
    if not olid or not isinstance(olid, str):
        return False
    return bool(olid.strip().startswith('OL') and olid.endswith('M'))

def check_openlibrary_full_text(olid: str) -> bool:
    """Check if book has full text available."""
    if not validate_olid(olid):
        return False

    params = {
        'bibkeys': f'OLID:{olid}',
        'jscmd': 'data',
        'format': 'json'
    }
    response = make_api_request(Config.OPEN_LIBRARY_API_URL, params)
    
    if response:
        book_data = response.json().get(f"OLID:{olid}", {})
        return "ocaid" in book_data
    return False

def download_openlibrary_text(conn: sqlite3.Connection, olid: str, identifier: str) -> bool:
    """Download and save full text content."""
    text_url = Config.OPEN_LIBRARY_TEXT_URL.format(identifier=identifier)
    response = make_api_request(text_url)

    if response:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO full_texts (book_id, text_content)
                VALUES ((SELECT id FROM books WHERE olid = ?), ?)
            """, (olid, response.text))
            conn.commit()
            logger.info(f"Full text saved for OLID {olid}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error saving text for {olid}: {e}")
            return False
    return False

def download_openlibrary_cover(conn: sqlite3.Connection, olid: str) -> bool:
    """Download and save cover image."""
    image_url = Config.OPEN_LIBRARY_COVER_URL.format(olid=olid)
    response = make_api_request(image_url)

    if response:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO images (book_id, image_type, image_data)
                VALUES ((SELECT id FROM books WHERE olid = ?), 'cover', ?)
            """, (olid, response.content))
            conn.commit()
            logger.info(f"Cover image saved for OLID {olid}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Database error saving image for {olid}: {e}")
            return False
    return False

def process_books_in_batches(conn: sqlite3.Connection, batch_size: int = Config.BATCH_SIZE):
    """Process books in batches."""
    cursor = conn.cursor()
    cursor.execute("SELECT olid FROM books WHERE source = 'Open Library'")
    
    processed = 0
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            break
            
        for (olid,) in batch:
            if not olid or not validate_olid(olid):
                continue
                
            logger.info(f"Processing Open Library book: {olid}")
            
            if check_openlibrary_full_text(olid):
                download_openlibrary_text(conn, olid, olid)
            download_openlibrary_cover(conn, olid)
            
            processed += 1
            if processed % 10 == 0:
                logger.info(f"Processed {processed} books")

    return processed

def import_csv_to_database(conn: sqlite3.Connection, csv_dir: str = "data/raw_csv") -> int:
    """Import books from CSV files into database."""
    imported = 0
    cursor = conn.cursor()

    try:
        csv_files = glob.glob(os.path.join(csv_dir, "*.csv"))
        logger.info(f"Found {len(csv_files)} CSV files to import")

        for csv_file in csv_files:
            try:
                df = pd.read_csv(csv_file)
                logger.info(f"Processing {csv_file} with {len(df)} records")

                for _, row in df.iterrows():
                    try:
                        # Extract OLID from any available field (modify based on your CSV structure)
                        olid = None
                        description = str(row.get('description', ''))
                        if 'OL' in description and 'M' in description:
                            # Try to extract OLID pattern
                            import re
                            olid_match = re.search(r'OL\d+M', description)
                            if olid_match:
                                olid = olid_match.group(0)

                        cursor.execute("""
                            INSERT OR IGNORE INTO books (olid, title, authors, source)
                            VALUES (?, ?, ?, ?)
                        """, (olid, row.get('title', ''), row.get('authors', ''), 'Open Library'))
                        
                        if cursor.rowcount > 0:
                            imported += 1
                            
                            # Log every 10th import
                            if imported % 10 == 0:
                                logger.info(f"Imported {imported} books...")
                    
                    except sqlite3.Error as e:
                        logger.error(f"Error importing row: {row.get('title', 'Unknown')} - {e}")
                        continue

                conn.commit()
                logger.info(f"Imported {imported} books from {csv_file}")

            except Exception as e:
                logger.error(f"Error processing {csv_file}: {e}")
                continue

        # Log final stats
        cursor.execute("SELECT COUNT(*) FROM books WHERE olid IS NOT NULL")
        books_with_olid = cursor.fetchone()[0]
        logger.info(f"Total books imported: {imported}")
        logger.info(f"Books with OLID: {books_with_olid}")
        
        return imported

    except Exception as e:
        logger.error(f"Import failed: {e}")
        raise

def fetch_missing_olids(conn: sqlite3.Connection) -> int:
    """Fetch OLIDs for books that don't have them."""
    cursor = conn.cursor()
    updated = 0
    batch_size = 10
    
    try:
        cursor.execute("SELECT id, title, authors FROM books WHERE olid IS NULL")
        books = cursor.fetchall()
        total_books = len(books)
        logger.info(f"Found {total_books} books without OLIDs")
        
        for i in range(0, total_books, batch_size):
            batch = books[i:i+batch_size]
            logger.info(f"Processing batch {i//batch_size + 1} of {(total_books + batch_size - 1)//batch_size}")
            
            for book_id, title, authors in batch:
                try:
                    # Try different search variations
                    search_variations = [
                        f"{title} {authors}".strip(),  # Full search
                        title.strip(),  # Just title
                        ' '.join(title.split()[:3])  # First 3 words of title
                    ]
                    
                    for search_query in search_variations:
                        params = {
                            'q': search_query,
                            'fields': 'key,title,author_name',
                            'limit': 1
                        }
                        
                        logger.info(f"Searching for: {search_query}")
                        response = make_api_request(Config.OPEN_LIBRARY_SEARCH_URL, params)
                        
                        if response and response.status_code == 200:
                            data = response.json()
                            num_found = data.get('num_found', 0)
                            logger.debug(f"Search '{search_query}' found {num_found} results")
                            if data.get('docs'):
                                logger.debug(f"First result: {data['docs'][0]}")
                                doc = data['docs'][0]
                                key = doc.get('key', '')
                                if key and isinstance(key, str):
                                    olid = key.split('/')[-1]
                                    if validate_olid(olid):
                                        cursor.execute(
                                            "UPDATE books SET olid = ? WHERE id = ?",
                                            (olid, book_id)
                                        )
                                        updated += 1
                                        logger.info(f"Found OLID {olid} for '{title}' using query: {search_query}")
                                        break  # Stop trying variations if we found a match
                        
                        time.sleep(Config.MIN_WAIT_BETWEEN_REQUESTS)
                    
                    # Commit every 10 updates
                    if updated % 10 == 0:
                        conn.commit()
                        logger.info(f"Committed {updated} updates")
                        
                except Exception as e:
                    logger.error(f"Error processing book '{title}': {e}")
                    continue
                
            # Commit at end of batch
            conn.commit()
            logger.info(f"Completed batch. Total updated: {updated}")
            
        logger.info(f"Finished processing. Updated {updated} books with OLIDs")
        return updated
        
    except Exception as e:
        logger.error(f"Error fetching OLIDs: {e}")
        return 0

if __name__ == "__main__":
    try:
        # Create data directories
        os.makedirs("data/raw_csv", exist_ok=True)
        os.makedirs("data/processed", exist_ok=True)

        # Remove existing database if it exists
        if os.path.exists(Config.DB_PATH):
            os.remove(Config.DB_PATH)
            logger.info(f"Removed existing database: {Config.DB_PATH}")

        with get_db_connection() as conn:
            init_database(conn)
            
            try:
                # Import CSV data
                total_imported = import_csv_to_database(conn)
                logger.info(f"Imported {total_imported} books from CSV files")

                # Fetch missing OLIDs
                updated_olids = fetch_missing_olids(conn)
                logger.info(f"Updated {updated_olids} books with OLIDs")

                # Process the imported books
                total_processed = process_books_in_batches(conn)
                logger.info(f"✅ Processing complete! Processed {total_processed} books")
                
            except sqlite3.Error as e:
                logger.error(f"Database operation failed: {e}")
                raise
            
    except Exception as e:
        logger.error(f"Script failed: {e}", exc_info=True)
        exit(1)
