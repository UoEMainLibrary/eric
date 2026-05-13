import sqlite3

import bootstrap
from project_paths import ERIC_DB

# Connect to your database
conn = sqlite3.connect(ERIC_DB)
cursor = conn.cursor()

# List tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print("Tables:", tables)

# Inspect a table, e.g., Object
cursor.execute("PRAGMA table_info(Object);")
columns = cursor.fetchall()
print("Object table columns:", columns)

# Check if any rows exist
cursor.execute("SELECT * FROM Object LIMIT 5;")
rows = cursor.fetchall()
print("Sample rows from Object:", rows)

conn.close()
