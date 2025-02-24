import json
from flask import Response
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.pool import NullPool
from functools import wraps
import os
import requests  # for API calls
from flask import Flask, session, render_template, request, redirect, url_for, flash, jsonify
from flask_session import Session
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Configuration
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

# Configure engine with connection pool settings
engine = create_engine(
    os.getenv("DATABASE_URL"),
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800  # Recycle connections after 30 minutes
)

# Database setup using SQLAlchemy ORM
db = scoped_session(sessionmaker(bind=engine))

# Close the database connection at the end of each request
@app.teardown_appcontext
def shutdown_session(exception=None):
    db.remove()

# Custom decorator for requiring login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page", "error")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# Helper function: Get data from Google Books API using ISBN
def get_google_books_data(isbn):

    book_api_key = os.getenv("BOOK_API_KEY")

    url = "https://www.googleapis.com/books/v1/volumes"
    params = {"q": f"isbn:{isbn}","key": book_api_key}
    try:
        res = requests.get(url, params=params)
        data = res.json()
        if data.get("totalItems", 0) > 0:
            volume_info = data["items"][0].get("volumeInfo", {})
            return volume_info
        else:
            return None
    except Exception as e:
        app.logger.error(f"Google Books API error: {e}")
        return None

# Helper function: Summarize text using Gemini API
def get_gemini_summary(text):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        return None
    gemini_url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": gemini_api_key}
    payload = {
        "contents": [{
            "parts": [{
                "text": f"summarize this text using less than 50 words: {text}"
            }]
        }]
    }
    try:
        res = requests.post(gemini_url, headers=headers, params=params, json=payload)
        data = res.json()
        if "candidates" in data and len(data["candidates"]) > 0:
            summary = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return summary
        else:
            return None
    except Exception as e:
        app.logger.error(f"Gemini API error: {e}")
        return None

@app.route("/")
@login_required
def index():
    # Get user details from database
    user = db.execute(
        text("SELECT username FROM users WHERE id = :user_id"),
        {"user_id": session["user_id"]}
    ).fetchone()
    
    return render_template("index.html", username=user.username)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        # Check if username exists
        existing_user = db.execute(
            text("SELECT id FROM users WHERE username = :username"),
            {"username": username}
        ).fetchone()

        if existing_user:
            flash("Username already exists", "error")
            return redirect(url_for("register"))

        # Insert new user
        hashed_password = generate_password_hash(password)
        db.execute(
            text("INSERT INTO users (username, password) VALUES (:username, :password)"),
            {"username": username, "password": hashed_password}
        )
        db.commit()

        flash("Registration successful! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        user = db.execute(
            text("SELECT * FROM users WHERE username = :username"),
            {"username": username}
        ).fetchone()

        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            flash("Logged in successfully!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid username or password", "error")
            return redirect(url_for("login"))

    return render_template("login.html")

@app.route("/search", methods=["GET", "POST"])
@login_required
def search():
    if request.method == "POST":
        search_query = request.form.get("query")
        search_type = request.form.get("search_type", "all")
        
        # Build the SQL query based on search type
        base_query = "SELECT * FROM books WHERE"
        params = {"query": f"%{search_query}%"}
        
        if search_type == "isbn":
            query = text(f"{base_query} isbn ILIKE :query")
        elif search_type == "title":
            query = text(f"{base_query} title ILIKE :query")
        elif search_type == "author":
            query = text(f"{base_query} author ILIKE :query")
        else:
            query = text("""
                SELECT * FROM books 
                WHERE isbn ILIKE :query 
                OR title ILIKE :query 
                OR author ILIKE :query
            """)
        
        books = db.execute(query, params).fetchall()
        
        return render_template("search.html", books=books, search_query=search_query)
    
    return render_template("search.html")

@app.route("/book/<isbn>")
@login_required
def book(isbn):
    """
    Display book details, reviews, and additional information from Google Books and Gemini API.
    """
    try:
        # Get book details from local database
        book = db.execute(
            text("SELECT * FROM books WHERE isbn = :isbn"),
            {"isbn": isbn}
        ).fetchone()

        if not book:
            return render_template("error.html", message="Book not found"), 404

        # Get all reviews for the book from our local database
        reviews = db.execute(
            text("""
                SELECT users.username, reviews.rating, reviews.review, reviews.created_at 
                FROM reviews
                JOIN users ON reviews.user_id = users.id
                WHERE book_id = :book_id
                ORDER BY reviews.created_at DESC
            """),
            {"book_id": book.id}
        ).fetchall()

        # Check if the current user has already reviewed this book
        user_review = db.execute(
            text("""
                SELECT * FROM reviews 
                WHERE user_id = :user_id AND book_id = :book_id
            """),
            {"user_id": session["user_id"], "book_id": book.id}
        ).fetchone()

        # Get additional book data from Google Books API
        google_data = get_google_books_data(isbn)
        title = google_data.get("title") if google_data else book.title
        author = google_data.get("authors", [None])[0] if google_data else book.author
        google_average_rating = google_data.get("averageRating") if google_data else None
        google_ratings_count = google_data.get("ratingsCount") if google_data else None
        google_published_date = google_data.get("publishedDate") if google_data else None

        isbn_10 = None
        isbn_13 = None
        if google_data and "industryIdentifiers" in google_data:
            for identifier in google_data["industryIdentifiers"]:
                if identifier.get("type") == "ISBN_10":
                    isbn_10 = identifier.get("identifier")
                elif identifier.get("type") == "ISBN_13":
                    isbn_13 = identifier.get("identifier")

        # Get book description from Google Books API and summarize it using Gemini API
        book_description = google_data.get("description") if google_data else None
        summarized_description = get_gemini_summary(book_description) if book_description else None

        return render_template("book.html", book=book, reviews=reviews, user_review=user_review,
                               title=title,author=author,
                               google_average_rating=google_average_rating,
                               google_ratings_count=google_ratings_count,
                               google_published_date=google_published_date,
                               isbn_10=isbn_10, isbn_13=isbn_13,
                               summarized_description=summarized_description)
    except Exception as e:
        app.logger.error(f"Error loading book page: {str(e)}")
        return render_template("error.html", message="An error occurred while loading the book details"), 500

@app.route("/submit-review", methods=["POST"])
@login_required
def submit_review():
    # Get form data
    isbn = request.form.get("isbn")
    rating = request.form.get("rating")
    review_text = request.form.get("review").strip()

    # Validate inputs
    if not all([isbn, rating, review_text]):
        flash("All fields are required", "error")
        return redirect(url_for("book", isbn=isbn))

    try:
        rating = int(rating)
        if not (1 <= rating <= 5):
            raise ValueError
    except ValueError:
        flash("Invalid rating value", "error")
        return redirect(url_for("book", isbn=isbn))

    try:
        book = db.execute(
            text("SELECT id FROM books WHERE isbn = :isbn"),
            {"isbn": isbn}
        ).fetchone()

        # Check for existing review
        existing_review = db.execute(
            text("""
                SELECT id FROM reviews 
                WHERE user_id = :user_id AND book_id = :book_id
            """),
            {"user_id": session["user_id"], "book_id": book.id}
        ).fetchone()

        if existing_review:
            flash("You've already submitted a review for this book", "error")
            return redirect(url_for("book", isbn=isbn))

        # Insert new review
        db.execute(
            text("""
                INSERT INTO reviews (user_id, book_id, rating, review)
                VALUES (:user_id, :book_id, :rating, :review)
            """),
            {
                "user_id": session["user_id"],
                "book_id": book.id,
                "rating": rating,
                "review": review_text
            }
        )
        db.commit()
        flash("Review submitted successfully!", "success")

    except Exception as e:
        db.rollback()
        flash("Error submitting review. Please try again.", "error")
        app.logger.error(f"Review submission error: {str(e)}")

    return redirect(url_for("book", isbn=isbn))

@app.route("/api/<isbn>")
def book_api(isbn):
    # Query book from local database
    book = db.execute(
        text("SELECT * FROM books WHERE isbn = :isbn"),
        {"isbn": isbn}
    ).fetchone()

    if not book:
        return jsonify({"error": "Book not found"}), 404

    # Get additional data from Google Books API
    google_data = get_google_books_data(isbn)
    title = google_data.get("title") if google_data else book.title
    author = google_data.get("authors", [None])[0] if google_data else book.author
    publishedDate = google_data.get("publishedDate") if google_data else None

    isbn_10 = None
    isbn_13 = None
    if google_data and "industryIdentifiers" in google_data:
        for identifier in google_data["industryIdentifiers"]:
            if identifier.get("type") == "ISBN_10":
                isbn_10 = identifier.get("identifier")
            elif identifier.get("type") == "ISBN_13":
                isbn_13 = identifier.get("identifier")

    rating_count = google_data.get("ratingsCount") if google_data else None
    averageRating = google_data.get("averageRating") if google_data else None

    # Count reviews in our local database for this book
    rating_count_from_local_database = db.execute(
        text("SELECT COUNT(*) FROM reviews WHERE book_id = :book_id"),
        {"book_id": book.id}
    ).scalar()

    # Get summarized description using Gemini API
    book_description = google_data.get("description") if google_data else None
    summarized_description = get_gemini_summary(book_description) if book_description else None

    result = {
        "title": title if title else None,
        "author": author if author else None,
        "publishedDate": publishedDate if publishedDate else None,
        "ISBN_10": isbn_10 if isbn_10 else None,
        "ISBN_13": isbn_13 if isbn_13 else None,
        "reviewCount": rating_count if rating_count else None,
        "review_count(local database)": rating_count_from_local_database if rating_count_from_local_database else None,
        "averageRating": averageRating if averageRating else None,
        "summarizedDescription": summarized_description if summarized_description else None
    }
    # Manually convert to JSON with proper key order
    result_json = json.dumps(result)

    # Return as a Response object
    return Response(result_json, mimetype='application/json')

# # New Route: Popular Books
# @app.route("/popular")
# @login_required
# def popular():
#     # Retrieve just 10  books from the local database for testing (for now i took just 10 books as the limit is 1000 requests)
#     books = db.execute(text("SELECT * FROM books limit 10")).fetchall()
#     popular_books = []
#     for book in books:
#         # Fetch Google Books data for each book
#         google_data = get_google_books_data(book.isbn)
#         if google_data and google_data.get("averageRating") is not None:
#             try:
#                 rating = float(google_data.get("averageRating"))
#             except Exception:
#                 rating = 0
#             ratings_count = google_data.get("ratingsCount")
#         else:
#             rating = 0
#             ratings_count = None
#         popular_books.append({
#             "id": book.id,
#             "isbn": book.isbn,
#             "title": book.title,
#             "author": book.author,
#             "year": book.year,
#             "google_rating": rating,
#             "google_ratings_count": ratings_count
#         })
#     # Sort books by Google average rating in descending order and select top 50
#     popular_books = sorted(popular_books, key=lambda x: x["google_rating"], reverse=True)[:50]
#     return render_template("popular.html", books=popular_books)


# Optimized 
@app.route("/popular")
@login_required
def popular():

    books = db.execute(text("SELECT * FROM books limit 10")).fetchall()
    # Define a helper function to process each book using execute() for the DB and raw API call for Google Books
    def process_book(book):
        google_data = get_google_books_data(book.isbn)
        if google_data and google_data.get("averageRating") is not None:
            try:
                rating = float(google_data.get("averageRating"))
            except Exception:
                rating = 0
            ratings_count = google_data.get("ratingsCount")
        else:
            rating = 0
            ratings_count = None
        return {
            "id": book.id,
            "isbn": book.isbn,
            "title": book.title,
            "author": book.author,
            "year": book.year,
            "google_rating": rating,
            "google_ratings_count": ratings_count
        }

    # Use ThreadPoolExecutor to fetch Google Books data concurrently
    popular_books = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(process_book, book) for book in books]
        for future in as_completed(futures):
            popular_books.append(future.result())

    # Sort books by Google average rating in descending order and select top 50
    popular_books = sorted(popular_books, key=lambda x: x["google_rating"], reverse=True)[:50]
    
    return render_template("popular.html", books=popular_books)



@app.route("/myreviews")
@login_required
def myreviews():
    # Query all reviews submitted by the logged-in user along with corresponding book details
    reviews = db.execute(text("""
        SELECT reviews.rating, reviews.review, reviews.created_at, books.title, books.author, books.isbn
        FROM reviews
        JOIN books ON reviews.book_id = books.id
        WHERE reviews.user_id = :user_id
        ORDER BY reviews.created_at DESC
    """), {"user_id": session["user_id"]}).fetchall()
    return render_template("myreviews.html", reviews=reviews)

@app.route("/logout")
@login_required
def logout():
    session.pop("user_id", None)
    flash("Logged out successfully!", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
