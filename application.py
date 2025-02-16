from sqlalchemy.pool import NullPool
from functools import wraps
import os
from flask import Flask, session, render_template, request, redirect, url_for, flash
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
    Display book details and reviews.
    If the current user has reviewed the book, show their review.
    Otherwise, show the review submission form.
    """
    try:
        # Get book details
        book = db.execute(
            text("SELECT * FROM books WHERE isbn = :isbn"),
            {"isbn": isbn}
        ).fetchone()

        # If book not found, return 404 error
        if not book:
            return render_template("error.html", message="Book not found"), 404

        # Get all reviews for the book
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

        return render_template("book.html", book=book, reviews=reviews, user_review=user_review)

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

@app.route("/logout")
@login_required
def logout():
    session.pop("user_id", None)
    flash("Logged out successfully!", "success")
    return redirect(url_for("login"))

if __name__ == "__main__":
    app.run(debug=True)
