import os
import threading
import logging
from flask import Flask, render_template, send_from_directory, redirect, url_for, flash, request
from flask_basicauth import BasicAuth
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from zeitung import ZeitungScraper, base_dir
import indexer

load_dotenv()

app = Flask(__name__)
# Secret Key aus ENV laden (Sicherheits-Standard)
app.secret_key = os.getenv('SECRET_KEY', 'fd3b45675922b44f06e2d31e4f32cdef')

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- AUTH SYSTEM ---
# Wir überschreiben BasicAuth, um 2 User zu erlauben
class MultiUserAuth(BasicAuth):
    def check_credentials(self, username, password):
        # 1. Check Admin
        if username == os.getenv('WEB_USER_ADMIN') and password == os.getenv('WEB_PASS_ADMIN'):
            return True
        # 2. Check Gast
        if username == os.getenv('WEB_USER_GUEST') and password == os.getenv('WEB_PASS_GUEST'):
            return True
        return False


# BasicAuth Config
app.config['BASIC_AUTH_FORCE'] = True  # Login immer erzwingen
basic_auth = MultiUserAuth(app)


def is_admin():
    """Hilfsfunktion: Prüft ob der aktuelle User der Admin ist"""
    # Wir holen uns die Daten direkt aus dem Browser-Request
    auth = request.authorization
    if not auth or not auth.username:
        return False
    return auth.username == os.getenv('WEB_USER_ADMIN')


# --- HINTERGRUND PROZESSE ---
process_lock = threading.Lock()
is_busy = False

# DB Init
if not os.path.exists('/app/downloads/zeitung.db'):
    indexer.init_db()
else:
    indexer.init_db()


def run_scraper_background():
    global is_busy
    is_busy = True
    try:
        logger.info("Starte Scraper...")
        scraper = ZeitungScraper()
        scraper.run()
        if scraper.target_path.exists():
            indexer.index_pdf(scraper.target_path)
    except Exception as e:
        logger.error(f"Scraper Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def run_reindex_background():
    global is_busy
    is_busy = True
    try:
        logger.info("Starte komplettes Re-Indexing...")
        indexer.rebuild_index(base_dir)
        logger.info("Re-Indexing fertig.")
    except Exception as e:
        logger.error(f"Reindex Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def try_start_process(target_func):
    if process_lock.acquire(blocking=False):
        thread = threading.Thread(target=target_func)
        thread.start()
        return True
    return False


# --- SCHEDULER JOBS ---
def job_download():
    logger.info("⏰ 06:00 - Auto-Download gestartet")
    try_start_process(run_scraper_background)


def job_reindex():
    logger.info("⏰ 06:15 - Auto-Reindex gestartet")
    try_start_process(run_reindex_background)


def run_archive_background(date_str, range_count):
    global is_busy
    is_busy = True
    try:
        logger.info(f"Starte Archiv Download: {date_str} (Range: {range_count})")
        scraper = ZeitungScraper()
        # Ruft die neue Methode auf und bekommt Liste der neuen Dateien
        new_files = scraper.run_archive(date_str, range_count)

        logger.info(f"Archiv Download fertig. {len(new_files)} Dateien geladen.")

        # Alle neuen Dateien sofort indexieren
        for fpath in new_files:
            if fpath.exists():
                logger.info(f"Indexiere {fpath.name}...")
                indexer.index_pdf(fpath)

    except Exception as e:
        logger.error(f"Archiv Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


scheduler = BackgroundScheduler()
# Job 1: Download um 06:00
scheduler.add_job(func=job_download, trigger="cron", hour=6, minute=0)
# Job 2: Reindex um 06:15
scheduler.add_job(func=job_reindex, trigger="cron", hour=6, minute=15)
scheduler.start()


# --- ROUTEN ---

@app.route('/')
def index():
    query = request.args.get('q', '').strip()

    if query:
        files = indexer.search_articles(query)
        flash(f'{len(files)} Treffer für "{query}" gefunden.', 'info')
    else:
        files = indexer.get_all_files(base_dir)

    # Wir übergeben is_admin an das Template
    return render_template('index.html',
                           files=files,
                           query=query,
                           is_scraping=is_busy,
                           admin_user=is_admin())


@app.route('/download/<filename>')
def download_file(filename):
    force_download = request.args.get('dl') == '1'
    response = send_from_directory(base_dir, filename, as_attachment=force_download)
    if not force_download:
        response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@app.route('/trigger-scrape')
def trigger_scrape():
    # SICHERHEIT: Nur Admin darf das
    if not is_admin():
        flash("Zugriff verweigert. Nur für Admins.", "danger")
        return redirect(url_for('index'))

    if try_start_process(run_scraper_background):
        flash('Download gestartet.', 'info')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/reindex')
def reindex():
    # SICHERHEIT: Nur Admin darf das
    if not is_admin():
        flash("Zugriff verweigert. Nur für Admins.", "danger")
        return redirect(url_for('index'))

    if try_start_process(run_reindex_background):
        flash('Re-Indexing gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/archive-download', methods=['POST'])
def archive_download():
    # SICHERHEIT: Nur Admin
    if not is_admin():
        flash("Nur für Admins.", "danger")
        return redirect(url_for('index'))

    date_str = request.form.get('date')  # YYYY-MM-DD
    range_val = int(request.form.get('range', 1))

    if not date_str:
        flash("Bitte ein Datum wählen.", "warning")
        return redirect(url_for('index'))

    if process_lock.acquire(blocking=False):
        thread = threading.Thread(target=run_archive_background, args=(date_str, range_val))
        thread.start()
        flash(f'Archiv-Download für {date_str} (+{range_val - 1} Tage) gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')

    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)