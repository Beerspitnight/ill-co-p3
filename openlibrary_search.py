import httpx  # Replace requests with httpx
import logging
from typing import List, Dict, Optional
from tenacity import retry, stop_after_attempt, wait_exponential

# Set up logging
logger = logging.getLogger(__name__)

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=30))
def fetch_books_from_openlibrary(query: str) -> List[Dict]:
    """
    Fetch books from Open Library API with improved retry logic.
    
    Args:
        query (str): Search query string
        
    Returns:
        List[Dict]: List of books with title, authors, and description
    """
    url = f"https://openlibrary.org/search.json?q={query}&limit=10"
    
    try:
        # Use httpx instead of requests
        with httpx.Client(timeout=30) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.json()
        
        books = []
        for doc in data.get("docs", []):
            book = {
                "title": doc.get("title", "Unknown Title"),
                "authors": doc.get("author_name", []),
                "description": None
            }
            
            # Try different fields for description
            for field in ["first_sentence", "description", "subtitle"]:
                if field in doc:
                    value = doc[field]
                    if isinstance(value, list):
                        book["description"] = value[0]
                    else:
                        book["description"] = value
                    break
            
            books.append(book)

        logger.info(f"Found {len(books)} books from OpenLibrary for query: {query}")
        return books
    except Exception as e:
        logger.error(f"Error fetching books from Open Library: {e}")
        return []
