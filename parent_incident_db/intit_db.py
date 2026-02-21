# init_db.py
import sqlite3
import os

DB_NAME = "incident_state.db"

def initialize_database():
    # Remove old db if you want a fresh start
    if os.path.exists(DB_NAME):
        print(f"Connecting to existing {DB_NAME}...")
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    with open("schema.sql", "r") as f:
        schema = f.read()
        
    cursor.executescript(schema)
    conn.commit()
    conn.close()
    print(f"✅ Database '{DB_NAME}' initialized successfully with Active_Problems table.")

if __name__ == "__main__":
    initialize_database()