import requests
import sqlite3
import time

# Google Books API Key (replace with your actual key)
GOOGLE_BOOKS_API_KEY = "YOUR_GOOGLE_BOOKS_API_KEY"

# Open Library Search URL
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json?q={query}&limit=5"

# Google Books Search URL
GOOGLE_BOOKS_SEARCH_URL = "https://www.googleapis.com/books/v1/volumes?q={query}&key=" + GOOGLE_BOOKS_API_KEY

# Connect to SQLite database
conn = sqlite3.connect("books.db")
cursor = conn.cursor()

# Function to fetch books from Open Library
def fetch_openlibrary_books(query):
    url = OPEN_LIBRARY_SEARCH_URL.format(query=query.replace(" ", "+"))
    response = requests.get(url)
    
    if response.status_code == 200:
        data = response.json()
        for book in data.get("docs", []):
            title = book.get("title")
            author = ", ".join(book.get("author_name", []))
            year = book.get("first_publish_year")
            olid = book.get("key").split("/")[-1] if "key" in book else None

            # Insert into database if public domain
            cursor.execute("""
                INSERT OR IGNORE INTO books (title, author, year, olid, source, license)
                VALUES (?, ?, ?, ?, 'Open Library', 'Public Domain')
            """, (title, author, year, olid))
        conn.commit()

# Function to fetch books from Google Books
def fetch_google_books(query):
    url = GOOGLE_BOOKS_SEARCH_URL.format(query=query.replace(" ", "+"))
    response = requests.get(url)

    if response.status_code == 200:
        data = response.json()
        for book in data.get("items", []):
            volume_info = book.get("volumeInfo", {})
            title = volume_info.get("title")
            author = ", ".join(volume_info.get("authors", []))
            year = volume_info.get("publishedDate", "").split("-")[0] if volume_info.get("publishedDate") else None
            isbn = next((id["identifier"] for id in volume_info.get("industryIdentifiers", []) if id["type"] == "ISBN_13"), None)

            # Check if book is public domain
            access_info = book.get("accessInfo", {})
            if access_info.get("publicDomain", False):
                cursor.execute("""
                    INSERT OR IGNORE INTO books (title, author, year, isbn, source, license)
                    VALUES (?, ?, ?, ?, 'Google Books', 'Public Domain')
                """, (title, author, year, isbn))
        conn.commit()

# List of Search Queries (Your Design Principle Topics)
search_queries = [
    "Balance in graphic design",
    "Contrast in graphic design",
    "Emphasis in visual design",
    "Visual hierarchy in design",
    "Alignment in graphic design",
    "Proximity in layout design",
    "Repetition in design systems",
    "Negative space in graphic design",
    "Typography principles in design",
    "Color theory in graphic design",
    "Scale and proportion in design",
    "Gestalt principles in UX design"
]

# Run searches
for query in search_queries:
    print(f"🔎 Searching: {query}")
    fetch_openlibrary_books(query)
    fetch_google_books(query)
    time.sleep(1)  # Avoid rate limits

# Close database connection
conn.close()

print("✅ Book fetching complete! Data stored in SQLite.")
