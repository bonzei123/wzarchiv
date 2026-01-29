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
    # FTS5 Tabelle für Volltextsuche
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS articles 
        USING fts5(filename, date, content)
    ''')
    conn.commit()
    conn.close()


def index_pdf(filepath):
    filename = filepath.name

    # Datumsextraktion robust machen
    try:
        date_str = filename.split('_')[0]
    except:
        date_str = "0000-00-00"

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Prüfen, ob Datei schon indexiert ist
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

    # FTS Suche: snippet() erstellt automatisch einen Auszug mit Highlighting
    safe_query = f'"{query}"'
    sql = """
        SELECT filename, date, snippet(articles, 2, '<mark>', '</mark>', '...', 20) as snippet
        FROM articles 
        WHERE articles MATCH ? 
        ORDER BY date DESC
    """

    try:
        c.execute(sql, (safe_query,))
        results = []
        for row in c.fetchall():
            r = dict(row)
            r['indexed'] = True
            results.append(r)
    except Exception as e:
        logger.error(f"Suchfehler: {e}")
        results = []

    conn.close()
    return results


def get_all_files(base_dir):
    """Liest Dateien vom Disk und prüft DB-Status"""

    # 1. Hole alle indexierten Dateinamen aus der DB
    db_filenames = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT filename FROM articles")
        db_filenames = {row[0] for row in c.fetchall()}
        conn.close()
    except Exception:
        pass  # Falls DB noch nicht existiert

    results = []

    # 2. Iteriere über echte Dateien im Ordner
    if base_dir.exists():
        for f in base_dir.glob("*.pdf"):
            filename = f.name
            try:
                date = filename.split('_')[0]
            except:
                date = "Unbekannt"

            is_indexed = filename in db_filenames

            results.append({
                'filename': filename,
                'date': date,
                'snippet': '',
                'indexed': is_indexed
            })

    # 3. Sortieren nach Datum (neueste zuerst)
    return sorted(results, key=lambda x: x['filename'], reverse=True)


def remove_orphaned_entries(base_dir):
    """Löscht Einträge aus der DB, die nicht mehr als Datei existieren"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Alle Dateien in der DB holen
    c.execute("SELECT filename FROM articles")
    db_files = c.fetchall()

    deleted_count = 0
    for (filename,) in db_files:
        file_path = base_dir / filename
        if not file_path.exists():
            logger.info(f"Entferne verwaisten Eintrag aus DB: {filename}")
            c.execute("DELETE FROM articles WHERE filename = ?", (filename,))
            deleted_count += 1

    if deleted_count > 0:
        conn.commit()
        logger.info(f"Bereinigung abgeschlossen. {deleted_count} Einträge entfernt.")

    conn.close()


def rebuild_index(base_dir):
    """Liest alle PDFs neu ein UND löscht alte Einträge"""
    init_db()

    # 1. Neue Dateien hinzufügen
    logger.info("Starte Indexierung vorhandener Dateien...")
    for pdf_file in base_dir.glob("*.pdf"):
        index_pdf(pdf_file)

    # 2. Alte Einträge löschen (Aufräumen)
    logger.info("Suche nach gelöschten Dateien...")
    remove_orphaned_entries(base_dir)