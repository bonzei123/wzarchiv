import sqlite3
import os
from pathlib import Path
from pypdf import PdfReader
import logging
from datetime import datetime
from pdf2image import convert_from_path

# Datenbank Datei
DB_PATH = Path('/app/downloads/zeitung.db')
THUMB_DIR = Path('/app/downloads/thumbnails')

logger = logging.getLogger(__name__)

# Sicherstellen, dass Thumbnail Ordner existiert
THUMB_DIR.mkdir(parents=True, exist_ok=True)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS articles 
        USING fts5(filename, date, content)
    ''')
    conn.commit()
    conn.close()


def format_german_date(date_str):
    """Wandelt 2026-01-30 in 'Freitag, 30. Januar 2026' um"""
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        days = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag']
        months = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober',
                  'November', 'Dezember']
        return f"{days[dt.weekday()]}, {dt.day}. {months[dt.month - 1]} {dt.year}"
    except:
        return date_str


def generate_thumbnail(pdf_path):
    """Erstellt ein JPG Thumbnail der ersten Seite"""
    try:
        thumb_filename = f"{pdf_path.stem}.jpg"
        thumb_path = THUMB_DIR / thumb_filename

        # Wenn Thumbnail schon existiert, überspringen
        if thumb_path.exists():
            return

        # Nur erste Seite konvertieren, 200dpi reicht für Thumbnails
        images = convert_from_path(str(pdf_path), first_page=1, last_page=1, dpi=200)
        if images:
            images[0].save(thumb_path, 'JPEG', quality=80)
            logger.info(f"Thumbnail erstellt: {thumb_filename}")

    except Exception as e:
        logger.error(f"Thumbnail Fehler für {pdf_path.name}: {e}")


def index_pdf(filepath):
    filename = filepath.name
    try:
        date_str = filename.split('_')[0]
    except:
        date_str = "0000-00-00"

    # 1. Thumbnail generieren (unabhängig von DB)
    generate_thumbnail(filepath)

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

            # Pfad und Größe
            fpath = Path('/app/downloads') / r['filename']
            if fpath.exists():
                r['size_mb'] = f"{fpath.stat().st_size / (1024 * 1024):.2f}"
            else:
                r['size_mb'] = "0.00"

            # Schönes Datum
            r['date_display'] = format_german_date(r['date'])

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

                year, week, _ = date_obj.isocalendar()
                week_id = f"{year}-W{week:02d}"

                # Formatierung nutzen
                date_display = format_german_date(date_str)

            except:
                date_str = "Unbekannt"
                date_display = filename
                week_id = "Unknown"

            is_indexed = filename in db_filenames
            size_mb = f"{f.stat().st_size / (1024 * 1024):.2f}"

            results.append({
                'filename': filename,
                'date': date_str,
                'date_display': date_display,  # Das schöne Datum für die Anzeige
                'week_id': week_id,
                'snippet': '',
                'indexed': is_indexed,
                'size_mb': size_mb
            })

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
            # Auch Thumbnail löschen
            thumb_path = THUMB_DIR / f"{Path(filename).stem}.jpg"
            if thumb_path.exists():
                try:
                    os.remove(thumb_path)
                except:
                    pass

    if deleted_count > 0:
        conn.commit()
    conn.close()


def rebuild_index(base_dir):
    init_db()
    # Sicherstellen, dass Thumbnails auch beim Rebuild erstellt werden
    for pdf_file in base_dir.glob("*.pdf"):
        index_pdf(pdf_file)
    remove_orphaned_entries(base_dir)