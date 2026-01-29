import sqlite3
import os
from pathlib import Path
from pypdf import PdfReader
import logging

# Datenbank Datei
DB_PATH = Path('/app/downloads/zeitung.db')

logger = logging.getLogger(__name__)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS articles 
        USING fts5(filename, date, content)
    ''')
    conn.commit()
    conn.close()


def index_pdf(filepath):
    filename = filepath.name
    date_str = filename.split('_')[0]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT rowid FROM articles WHERE filename = ?", (filename,))
    if c.fetchone():
        conn.close()
        return

    try:
        logger.info(f"Indiziere: {filename} ...")
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            extract = page.extract_text()
            if extract:
                text += extract + " "

        c.execute("INSERT INTO articles (filename, date, content) VALUES (?, ?, ?)",
                  (filename, date_str, text))
        conn.commit()
        logger.info(f"Erfolgreich indexiert: {filename}")
    except Exception as e:
        logger.error(f"Fehler beim Lesen von {filename}: {e}")
    finally:
        conn.close()


def search_articles(query):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # FTS Suche
    safe_query = f'"{query}"'
    sql = """
        SELECT filename, date, snippet(articles, 2, '<mark>', '</mark>', '...', 20) as snippet
        FROM articles 
        WHERE articles MATCH ? 
        ORDER BY date DESC
    """
    c.execute(sql, (safe_query,))

    # Ergebnisse umwandeln und Flag setzen
    results = []
    for row in c.fetchall():
        r = dict(row)
        r['indexed'] = True  # Suchergebnisse sind per Definition indexiert
        results.append(r)

    conn.close()
    return results


# === HIER IST DIE WICHTIGE ÄNDERUNG ===
def get_all_files(base_dir):
    """Liest Dateien vom Disk und prüft DB-Status"""

    # 1. Hole alle indexierten Dateinamen aus der DB
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Wir brauchen nur den Dateinamen zum Abgleich
    c.execute("SELECT filename FROM articles")
    db_filenames = {row['filename'] for row in c.fetchall()}
    conn.close()

    results = []

    # 2. Iteriere über echte Dateien im Ordner
    if base_dir.exists():
        for f in base_dir.glob("*.pdf"):
            filename = f.name

            # Datum aus Dateinamen extrahieren (Format: YYYY-MM-DD_...)
            try:
                date = filename.split('_')[0]
            except:
                date = "Unbekannt"

            is_indexed = filename in db_filenames

            results.append({
                'filename': filename,
                'date': date,
                'snippet': '',
                'indexed': is_indexed  # True oder False
            })

    # 3. Sortieren nach Datum (neueste zuerst)
    # Wir nutzen filename als fallback sortierung
    return sorted(results, key=lambda x: x['filename'], reverse=True)


def rebuild_index(download_dir):
    init_db()
    for pdf_file in download_dir.glob("*.pdf"):
        index_pdf(pdf_file)