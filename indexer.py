import sqlite3
import os
from pathlib import Path
from pypdf import PdfReader
import logging
from datetime import datetime

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
    try:
        date_str = filename.split('_')[0]
    except:
        date_str = "0000-00-00"

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
            # Größe holen (für Suchergebnisse auch anzeigen)
            fpath = Path('/app/downloads') / r['filename']
            if fpath.exists():
                r['size_mb'] = f"{fpath.stat().st_size / (1024 * 1024):.2f}"
            else:
                r['size_mb'] = "0.00"
            results.append(r)
    except Exception as e:
        logger.error(f"Suchfehler: {e}")
        results = []

    conn.close()
    return results


def get_all_files(base_dir):
    """
    Liest alle Dateien und reichert sie mit Metadaten an.
    """
    # DB Status holen
    db_filenames = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT filename FROM articles")
        db_filenames = {row[0] for row in c.fetchall()}
        conn.close()
    except Exception:
        pass

    results = []

    if base_dir.exists():
        for f in base_dir.glob("*.pdf"):
            filename = f.name
            try:
                date_str = filename.split('_')[0]
                date_obj = datetime.strptime(date_str, '%Y-%m-%d')

                # Wochen-Info für Filterung berechnen
                # Format: "2026-W05"
                year, week, _ = date_obj.isocalendar()
                week_id = f"{year}-W{week:02d}"

            except:
                date_str = "Unbekannt"
                week_id = "Unknown"

            is_indexed = filename in db_filenames

            # Größe berechnen
            size_mb = f"{f.stat().st_size / (1024 * 1024):.2f}"

            results.append({
                'filename': filename,
                'date': date_str,
                'week_id': week_id,
                'snippet': '',
                'indexed': is_indexed,
                'size_mb': size_mb
            })

    # Sortieren: Neueste zuerst
    return sorted(results, key=lambda x: x['filename'], reverse=True)


def remove_orphaned_entries(base_dir):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT filename FROM articles")
    db_files = c.fetchall()

    deleted_count = 0
    for (filename,) in db_files:
        file_path = base_dir / filename
        if not file_path.exists():
            c.execute("DELETE FROM articles WHERE filename = ?", (filename,))
            deleted_count += 1

    if deleted_count > 0:
        conn.commit()
    conn.close()


def rebuild_index(base_dir):
    init_db()
    for pdf_file in base_dir.glob("*.pdf"):
        index_pdf(pdf_file)
    remove_orphaned_entries(base_dir)