import os
import threading
import logging
import fcntl
import atexit
from flask import Flask, render_template, send_from_directory, redirect, url_for, flash, request
from flask_basicauth import BasicAuth
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from zeitung import ZeitungScraper, base_dir
import indexer

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_key')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# --- AUTH SYSTEM ---
class MultiUserAuth(BasicAuth):
    def check_credentials(self, username, password):
        if username == os.getenv('WEB_USER_ADMIN') and password == os.getenv('WEB_PASS_ADMIN'):
            return True
        if username == os.getenv('WEB_USER_GUEST') and password == os.getenv('WEB_PASS_GUEST'):
            return True
        return False


app.config['BASIC_AUTH_FORCE'] = True
basic_auth = MultiUserAuth(app)


def is_admin():
    auth = request.authorization
    if not auth or not auth.username:
        return False
    return auth.username == os.getenv('WEB_USER_ADMIN')


# --- HINTERGRUND PROZESSE ---
process_lock = threading.Lock()
is_busy = False

# DB Init
if not os.path.exists('/app/downloads/zeitung.db'):
    try:
        indexer.init_db()
    except Exception as e:
        logger.error(f"DB Init Fehler: {e}")
else:
    indexer.init_db()


def run_scraper_background():
    global is_busy
    is_busy = True
    try:
        logger.info("Starte Scraper...")
        scraper = ZeitungScraper()
        scraper.run()

        # FIX: Prüfen ob target_path gesetzt ist, bevor wir darauf zugreifen
        # Das verhindert den "NoneType has no attribute exists" Fehler
        if scraper.target_path and scraper.target_path.exists():
            indexer.index_pdf(scraper.target_path)
        else:
            logger.warning("Scraper beendet, aber keine Datei zum Indexieren gefunden.")

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


def run_archive_background(date_str, range_count):
    global is_busy
    is_busy = True
    try:
        logger.info(f"Starte Archiv Download: {date_str} (Range: {range_count})")
        scraper = ZeitungScraper()
        new_files = scraper.run_archive(date_str, range_count)

        logger.info(f"Archiv Download fertig. {len(new_files)} Dateien geladen.")

        for fpath in new_files:
            if fpath and fpath.exists():
                logger.info(f"Indexiere {fpath.name}...")
                indexer.index_pdf(fpath)

    except Exception as e:
        logger.error(f"Archiv Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def try_start_process(target_func, *args):
    if process_lock.acquire(blocking=False):
        thread = threading.Thread(target=target_func, args=args)
        thread.start()
        return True
    return False


# --- SCHEDULER (SINGLETON) ---
# Wir nutzen fcntl um sicherzustellen, dass nur EINER der 4 Worker den Scheduler startet.
# Sonst starten 4 Downloads gleichzeitig und crashen den Chrome-Treiber.

f = open("scheduler.lock", "wb")
try:
    # Versuche exklusiven, nicht-blockierenden Lock zu bekommen
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


    # WENN ERFOLGREICH: Wir sind der Master-Worker
    def job_download():
        logger.info("⏰ 06:00 - Auto-Download gestartet")
        try_start_process(run_scraper_background)


    def job_reindex():
        logger.info("⏰ 06:15 - Auto-Reindex gestartet")
        try_start_process(run_reindex_background)


    scheduler = BackgroundScheduler()
    scheduler.add_job(func=job_download, trigger="cron", hour=6, minute=0, id='job_download')
    scheduler.add_job(func=job_reindex, trigger="cron", hour=6, minute=15, id='job_reindex')
    scheduler.start()
    logger.info("✅ Scheduler erfolgreich in diesem Worker gestartet.")


    # Cleanup beim Beenden
    def unlock():
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


    atexit.register(unlock)

except IOError:
    # Lock fehlgeschlagen -> Ein anderer Worker macht schon den Job
    logger.info("ℹ️ Scheduler läuft bereits in einem anderen Worker. Überspringe.")


# --- ROUTEN ---

@app.route('/')
def index():
    query = request.args.get('q', '').strip()

    if query:
        files = indexer.search_articles(query)
        flash(f'{len(files)} Treffer für "{query}" gefunden.', 'info')
    else:
        files = indexer.get_all_files(base_dir)

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
    if not is_admin():
        flash("Zugriff verweigert.", "danger")
        return redirect(url_for('index'))

    if try_start_process(run_scraper_background):
        flash('Download gestartet.', 'info')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/reindex')
def reindex():
    if not is_admin():
        flash("Zugriff verweigert.", "danger")
        return redirect(url_for('index'))

    if try_start_process(run_reindex_background):
        flash('Re-Indexing gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/archive-download', methods=['POST'])
def archive_download():
    if not is_admin():
        flash("Nur für Admins.", "danger")
        return redirect(url_for('index'))

    date_str = request.form.get('date')
    try:
        range_val = int(request.form.get('range', 1))
    except:
        range_val = 1

    if not date_str:
        flash("Bitte ein Datum wählen.", "warning")
        return redirect(url_for('index'))

    if try_start_process(run_archive_background, date_str, range_val):
        flash(f'Archiv-Download für {date_str} (+{range_val - 1} Tage) gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')

    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)