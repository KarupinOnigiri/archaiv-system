from database import get_db_connection
conn = get_db_connection()
conn.execute("INSERT INTO categories (name) VALUES (?)", ("Hand Tools",))
conn.execute("INSERT INTO categories (name) VALUES (?)", ("Power Tools",))
conn.execute("INSERT INTO categories (name) VALUES (?)", ("Measuring Instruments",))
conn.commit()
conn.close()
print("done")
