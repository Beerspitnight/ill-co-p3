from flask import Flask, request, jsonify, g, redirect, url_for, Blueprint, send_from_directory, make_response
from werkzeug.utils import safe_join
from datetime import datetime
import os
import requests
import base64
import binascii
import json
import logging
import csv
import tempfile
import re
import uuid
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from pydantic import BaseModel
from google.oauth2 import service_account
from flask_compress import Compress
from pydantic_settings import BaseSettings
from tenacity import retry, stop_after_attempt, wait_exponential
from functools import lru_cache
from ratelimit import limits, sleep_and_retry, RateLimitException
from openlibrary_search import fetch_books_from_openlibrary

# Define BookResponse model
class BookResponse(BaseModel):
    title: str
    authors: list[str]
    description: str | None = None

    class Config:
        schema_extra = {
            "example": {
                "title": "The Great Gatsby",
                "authors": ["F. Scott Fitzgerald"],
                "description": "A story of the American dream...",
                "categories": ["Fiction", "Classic"],
                "publisher": "Scribner"
            }
        }

# Define Settings model
class Settings(BaseSettings):
    """
    Configuration settings for the application.

    Attributes:
        GOOGLE_BOOKS_API_KEY (str): API key for accessing the Google Books API.
        GOOGLE_APPLICATION_CREDENTIALS (str): Path or content of Google service account credentials.
        MAX_RETRIES (int): Maximum number of retries for API requests. Default is 3.
        CACHE_TIMEOUT (int): Cache timeout duration in seconds. Default is 3600 seconds (1 hour).
    """
    GOOGLE_BOOKS_API_KEY: str
    GOOGLE_APPLICATION_CREDENTIALS: str
    MAX_RETRIES: int = 3
    CACHE_TIMEOUT: int = 3600

    class Config:
        env_file = '.env'

# Initialize settings
def get_settings():
    return Settings()
def register_routes(api_v1):
    # TODO: Implement route registration logic here
    pass
# Initialize Blueprint
api_v1 = Blueprint('api_v1', __name__, url_prefix='/api/v1')

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define function to register routes
def register_routes(api_v1):
    """Registers all the routes for the api_v1 blueprint."""
    pass

# Define create_app function
def create_app():
    """Application factory pattern"""
    app = Flask(__name__)
    settings = get_settings()

    configure_app(app, settings)
    initialize_extensions(app)
    setup_routes(app)

    return app, settings

def configure_app(app, settings):
    """Configure the Flask app with settings."""
    app.config.update(
        GOOGLE_BOOKS_API_KEY=settings.GOOGLE_BOOKS_API_KEY,
        GOOGLE_APPLICATION_CREDENTIALS=settings.GOOGLE_APPLICATION_CREDENTIALS,
        MAX_RETRIES=settings.MAX_RETRIES,
        CACHE_TIMEOUT=settings.CACHE_TIMEOUT
    )
    app.config['RESULTS_DIR'] = os.path.join(os.getcwd(), "learning", "Results")
    os.makedirs(app.config['RESULTS_DIR'], exist_ok=True)

def initialize_extensions(app):
    """Initialize Flask extensions."""
    compress = Compress()
    compress.init_app(app)

def setup_routes(app):
    """Set up routes and blueprints."""
    # Register the blueprint routes
    register_routes(api_v1)
    app.register_blueprint(api_v1)

    # Register core routes
    @app.route("/")
    def index():
        return "<h1>Welcome to the LibraryCloud API!</h1>"

    @app.route("/test_drive")
    def test_drive():
        try:
            service = get_drive_service()
            about = service.about().get(fields="user,storageQuota").execute()
            return jsonify({
                "success": True,
                "user": about.get("user", {}),
                "quota": about.get("storageQuota", {})
            })
        except Exception as e:
            logger.error(f"Drive test failed: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

    @app.route("/verify_credentials")
    def verify_credentials():
        """Verify Google Drive credentials configuration."""
        try:
            creds = app.config['GOOGLE_APPLICATION_CREDENTIALS']
            # Try to decode if it's base64
            try:
                decoded = base64.b64decode(creds).decode('utf-8') if creds else None
            except:
                decoded = None
                
            return jsonify({
                "success": True,
                "credentials_set": bool(creds),
                "credentials_type": str(type(creds)),
                "credentials_length": len(str(creds)) if creds else 0,
                "credentials_preview": str(creds)[:50] + "..." if creds else None,
                "decoded_preview": str(decoded)[:50] + "..." if decoded else None
            })
        except Exception as e:
            logger.error(f"Credential verification failed: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": str(e)
            }), 500

# Create app instance
app, settings = create_app()

@app.route("/search_books", methods=["GET"])
def search_books():
    query = request.args.get("query", "").strip()
    if not query or len(query) < 2:
        return jsonify({"error": "Query must be at least 2 characters"}), 400

    # Add pagination
    per_page = request.args.get("per_page", default=10, type=int)
    if per_page < 1 or per_page > 40:  # Google Books API limit
        return jsonify({"error": "per_page must be between 1 and 40"}), 400

    # Fetch books
    try:
        books = fetch_books_from_google(query)

        # Validate and structure the book data
        validated_books = []
        for book in books:
            try:
                try:
                    # Filter the book dictionary to include only valid fields for BookResponse
                    valid_fields = BookResponse.__fields__.keys()
                    filtered_book = {key: value for key, value in book.items() if key in valid_fields}
                    validated_books.append(BookResponse(**filtered_book).model_dump())
                except Exception as e:
                    logger.error(f"Validation error for book data: {book}. Error: {e}")
                    continue
            except Exception as e:
                logger.error(f"Validation error for book data: {book}. Error: {e}")
                continue

        # Fix: Use save_results_to_csv for local storage and then optionally upload to Drive
        csv_filename = save_results_to_csv(validated_books, query)
        if csv_filename is None:
            return jsonify({"error": "Failed to save results to CSV"}), 500

        # Optionally upload to Google Drive if needed
        drive_link = None
        try:
            drive_link = upload_search_results_to_drive(validated_books, query)
        except Exception as e:
            logger.warning(f"Failed to upload to Google Drive: {e}")
            # Continue even if Drive upload fails

        return jsonify({
            "books": validated_books, 
            "csv_filename": csv_filename,
            "csv_link": drive_link
        })
    except ValueError as ve:
        logger.error(f"ValueError in search_books: {ve}")
        return jsonify({"error": "Invalid input"}), 400
    except Exception as e:
        logger.exception(f"Unexpected error in search_books: {e}")
        return jsonify({"error": "An unexpected error occurred while searching for books"}), 500

@app.route("/list_results")
def list_results():
    try:
        # Fix: Use app.config to get RESULTS_DIR
        results_dir = app.config['RESULTS_DIR']
        if not os.path.exists(results_dir):
            logger.warning(f"Results directory does not exist: {results_dir}")
            return jsonify({
                "error": "Results directory does not exist",
                "directory": results_dir
            }), 404

        # List all CSV files in the results directory
        result_files = [f for f in os.listdir(results_dir) if f.endswith('.csv')]
        return jsonify({
            "files": result_files,
            "count": len(result_files),
            "directory": results_dir
        })
    except Exception as e:
        logger.exception(f"Error listing results: {str(e)}")
        return jsonify({"error": "An unexpected error occurred while listing results"}), 500

@app.route("/get_file")
def get_file():
    filename = request.args.get("filename")
    if not filename:
        return jsonify({"error": "Filename parameter is required"}), 400

    results_dir = app.config['RESULTS_DIR']
    logger.info(f"Looking for file {filename} in directory {results_dir}")

    try:
        # Validate filename
        if not re.match(r'^[a-zA-Z0-9_.-]+$', filename):
            return jsonify({"error": "Invalid filename format"}), 400
            
        filepath = os.path.join(results_dir, filename)
        
        # Check if file exists
        if not os.path.exists(filepath):
            logger.error(f"File not found: {filepath}")
            return jsonify({"error": "File not found"}), 404
            
        # Prevent directory traversal
        if os.path.commonpath([filepath, results_dir]) != os.path.normpath(results_dir):
            logger.error(f"Security issue: Attempted to access file outside results directory")
            return jsonify({"error": "Security error"}), 403

        # Log file size
        file_size = os.path.getsize(filepath)
        logger.info(f"Serving file {filepath} with size {file_size} bytes")
        
        # Read file in binary mode
        with open(filepath, 'rb') as f:
            response = make_response(f.read())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response

    except Exception as e:
        logger.exception(f"Error serving file {filename}: {e}")
        return jsonify({"error": f"Error serving file: {str(e)}"}), 500

@app.route("/search_openlibrary")
def search_openlibrary():
    """Search books using OpenLibrary API."""
    query = request.args.get("query")
    if not query:
        return jsonify({"error": "Query parameter is required"}), 400
        
    try:
        # Log the incoming request
        request_id = str(uuid.uuid4())
        logger.info(f"Processing request {request_id}: GET /search_openlibrary")
        
        # Fetch books from OpenLibrary
        books = fetch_books_from_openlibrary(query)
        
        if not books:
            return jsonify({
                "message": "No books found",
                "books": [],
                "drive_link": None
            })
            
        # Upload results to Google Drive
        drive_link = upload_search_results_to_drive(books, f"openlibrary_{query}")
        
        return jsonify({
            "message": f"Found {len(books)} books",
            "books": books,
            "drive_link": drive_link
        })
        
    except Exception as e:
        logger.error(f"Error processing OpenLibrary search: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# Define before_request function
@app.before_request
def before_request():
    g.request_id = request.headers.get('X-Request-ID', str(uuid.uuid4()))
    logger.info(f"Processing request {g.request_id}: {request.method} {request.path}")

# Validate GOOGLE_BOOKS_API_KEY
GOOGLE_BOOKS_API_KEY = settings.GOOGLE_BOOKS_API_KEY
if not GOOGLE_BOOKS_API_KEY.strip():
    raise RuntimeError("GOOGLE_BOOKS_API_KEY is not set. Application cannot start without it.")

def filter_book_data(volume_info):
    """Filter and format book data from Google Books API response."""
    return {
        "title": volume_info.get("title", ""),
        "authors": volume_info.get("authors", []),
        "description": volume_info.get("description", None)
    }

# Define fetch_books_from_google function
@lru_cache(maxsize=128)
@sleep_and_retry
@limits(calls=100, period=60)  # Add rate limiting
def fetch_books_from_google(query):
    """Fetch books from Google Books API with rate limiting and caching.

    Uses an LRU cache to store results for repeated queries.
    """
    if not query or not isinstance(query, str) or len(query.strip()) == 0:
        raise ValueError("Query parameter must be a non-empty string.")

    from urllib.parse import quote
    url = f"https://www.googleapis.com/books/v1/volumes?q={quote(query)}&key={GOOGLE_BOOKS_API_KEY}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        return [filter_book_data(item["volumeInfo"]) for item in data.get("items", [])]
    except RateLimitException as e:
        logger.warning(f"Rate limit exceeded: {e}")
        raise
    except Exception as e:
        logger.error(f"Error fetching books: {e}")
        raise
if app.config.get("DEBUG", False):
    logger.debug(f"Application root: {os.path.dirname(__file__)}")
# Log application details
logger.info(f"Application root: {os.path.dirname(__file__)}")
logger.info(f"Running on Heroku: {bool(os.getenv('HEROKU'))}")

# Define get_drive_service function
def save_results_to_csv(books, query):
    """Save search results to a CSV file in the results directory.
    
    Args:
        books (list): List of book dictionaries
        query (str): Search query string
        
    Returns:
        str: Filename of the saved CSV file or None if failed
    """
    if not books:
        logger.warning("No books found for the given query.")
        return None

    try:
        filename = f'search_results_{query.replace(" ", "_")}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        filepath = os.path.join(app.config['RESULTS_DIR'], filename)
        
        with open(filepath, "w", newline="", encoding="utf-8") as file:
            fieldnames = ["title", "authors", "description"]
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for book in books:
                book_row = {field: book.get(field, "") for field in fieldnames}
                if isinstance(book_row.get('authors'), list):
                    book_row['authors'] = ', '.join(book_row['authors'])
                writer.writerow(book_row)
        
        return filename
        
    except Exception as e:
        logger.error(f"Error saving results to CSV: {e}", exc_info=True)
        return None

def get_drive_service():
    """Returns an authenticated Google Drive service object."""
    google_credentials = app.config['GOOGLE_APPLICATION_CREDENTIALS']
    if not google_credentials:
        logger.error("GOOGLE_APPLICATION_CREDENTIALS environment variable is not set")
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    
    logger.info(f"Credential type: {type(google_credentials)}")
    logger.info(f"Credential length: {len(google_credentials)}")
    
    try:
        # First, try to decode base64
        try:
            logger.info("Attempting base64 decode...")
            # Remove any whitespace/newlines that might have been added
            cleaned_creds = google_credentials.strip()
            creds_json = base64.b64decode(cleaned_creds).decode('utf-8')
            logger.info("Successfully decoded base64")
            credentials_info = json.loads(creds_json)
            logger.info("Successfully parsed JSON after base64 decode")
        except (binascii.Error, json.JSONDecodeError) as e:
            logger.warning(f"Base64 decode failed: {str(e)}")
            # If base64 fails, try direct JSON parsing
            try:
                logger.info("Attempting direct JSON parse...")
                credentials_info = json.loads(google_credentials)
                logger.info("Successfully parsed JSON directly")
            except json.JSONDecodeError as e:
                logger.error(f"All parsing attempts failed: {str(e)}")
                raise GoogleDriveError(f"Could not parse credentials: {str(e)}")

        # Create credentials object with specific scope
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/drive.file"]
        )

        # Build and return the service
        service = build('drive', 'v3', credentials=credentials)
        logger.info("Successfully created Google Drive service")
        return service

    except Exception as e:
        logger.error(f"Failed to create Drive service: {str(e)}", exc_info=True)
        raise GoogleDriveError(f"Drive service creation failed: {str(e)}")

# Define custom exceptions
class GoogleDriveError(Exception):
    """Custom exception for Google Drive operations"""
    pass

class BookAPIError(Exception):
    """Custom exception for Google Books API operations"""
    pass

# Define upload_to_google_drive function
def upload_to_google_drive(file_path, file_name):
    """Uploads a file to Google Drive with enhanced logging."""
    if not os.path.exists(file_path):
        logger.error(f"File not found at path: {file_path}")
        raise GoogleDriveError(f"File not found: {file_path}")

    logger.info(f"Starting upload process for file: {file_name}")
    logger.info(f"File path: {file_path}")
    logger.info(f"File size: {os.path.getsize(file_path)} bytes")

    try:
        logger.info("Getting Drive service...")
        service = get_drive_service()
        
        logger.info("Creating file metadata with parent folder...")
        file_metadata = {
            'name': file_name,
            'parents': ['1q8Rbo5N3mPweYlrf3rFFXxLGUbW95o-j']  # Specified folder ID
        }
        
        logger.info("Creating MediaFileUpload object...")
        media = MediaFileUpload(file_path, resumable=True)
        
        logger.info("Executing file creation...")
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        file_id = file.get('id')

        if not file_id:
            logger.error("No file ID received after upload")
            raise GoogleDriveError("Failed to get file ID after upload")

        logger.info(f"File uploaded successfully with ID: {file_id}")

        # Make the file publicly accessible
        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
        def set_file_permissions():
            logger.info(f"Setting permissions for file ID: {file_id}")
            
            # Make file public
            public_permission = service.permissions().create(
                fileId=file_id,
                body={
                    "role": "writer",  # Changed from reader to writer
                    "type": "anyone"
                }
            ).execute()
            logger.info(f"Public permission result: {public_permission}")

            # Add specific user permission
            user_permission = service.permissions().create(
                fileId=file_id,
                body={
                    "role": "writer",
                    "type": "user",
                    "emailAddress": "iwasonamountian@gmail.com"
                },
                sendNotificationEmail=False
            ).execute()
            logger.info(f"User permission result: {user_permission}")

        try:
            set_file_permissions()
            share_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
            logger.info(f"File shared successfully. Link: {share_link}")
            return share_link
        except Exception as e:
            logger.error(f"Error setting file permissions: {str(e)}", exc_info=True)
            # Return the link even if permission setting fails
            return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    except Exception as e:
        logger.error(f"Error in upload_to_google_drive: {str(e)}", exc_info=True)
        return None

# Fix: A new function to handle both saving to CSV and uploading to Drive
def upload_search_results_to_drive(books, query):
    """Saves books to a temporary CSV file and uploads to Google Drive."""
    if not books:
        logger.warning("No books found for the given query.")
        return None

    temp_file = None
    try:
        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as temp:
            temp_file = temp.name
        
        file_name = f'search_results_{query.replace(" ", "_")}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
        
        # Write data to temp file
        with open(temp_file, "w", newline="", encoding="utf-8") as file:
            fieldnames = ["title", "authors", "description"]
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for book in books:
                # Handle authors list conversion and provide default values
                book_row = {field: book.get(field, "") for field in fieldnames}
                if isinstance(book_row.get('authors'), list):
                    book_row['authors'] = ', '.join(book_row['authors'])
                writer.writerow(book_row)  # Write only once
        
        # Upload to Google Drive
        return upload_to_google_drive(temp_file, file_name)
        
    except Exception as e:
        logger.error(f"Error in upload_search_results_to_drive: {e}", exc_info=True)
        return None
    finally:
        if temp_file and os.path.exists(temp_file):
            os.unlink(temp_file)

# Define validate_port function
def validate_port(port_str):
    if not port_str.isdigit():
        raise RuntimeError(f"Invalid PORT environment variable: {port_str}. Must be a numeric value.")
    port = int(port_str)
    if port <= 0 or port > 65535:
        raise ValueError("Port number must be between 1 and 65535.")
    return port

# Run the app
if __name__ == "__main__":
    port_env = os.environ.get("PORT", "5000")
    try:
        port = validate_port(port_env)
        debug_mode = os.environ.get("FLASK_ENV", "production") == "development"
        app.run(host="0.0.0.0", port=port, debug=debug_mode)
    except (ValueError, RuntimeError) as e:
        logger.error(f"Failed to start application: {e}")
        raise