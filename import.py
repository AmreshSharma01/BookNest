import csv
import os

from flask import Flask, session
from flask_session import Session
from sqlalchemy import create_engine,text
from sqlalchemy.orm import scoped_session, sessionmaker

app = Flask(__name__)

# Check for environment variable
if not os.getenv("DATABASE_URL"):
    raise RuntimeError("DATABASE_URL is not set")


# Set up database
engine = create_engine(os.getenv("DATABASE_URL"))
db = scoped_session(sessionmaker(bind=engine))

def main():
    f=open("books.csv")
    reader=csv.reader(f)
    next(reader)  # Skip header
    for isbn, title, author, year in reader:
        db.execute(
                text("INSERT INTO books (isbn, title, author, year) VALUES (:isbn, :title, :author, :year)"),
                {"isbn": isbn, "title": title, "author": author, "year": int(year)}
        )
    db.commit()
    print("Books imported successfully.")

if __name__=="__main__":
    main()

