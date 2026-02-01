import os
import threading
import logging
import fcntl  # NEU: Für File-Locking
import atexit  # NEU: Zum Aufräumen
from datetime import datetime, timedelta
from flask import Flask, render_template, send_from_directory, redirect, url_for, flash, request
from flask_basicauth import BasicAuth
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from zeitung import ZeitungScraper, base_dir
import indexer

# Kompressor für manuelle Ausführung importieren
try:
    from compressor import compress_pdf
except ImportError:
    def compress_pdf(path):
        return False

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev_key')

# Logger
logging.basicConfig(level=logging.INFO)
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
        if scraper.target_path and scraper.target_path.exists():
            indexer.index_pdf(scraper.target_path)
    except Exception as e:
        logger.error(f"Scraper Fehler: {e}")
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

        for fpath in new_files:
            if fpath.exists():
                indexer.index_pdf(fpath)

    except Exception as e:
        logger.error(f"Archiv Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def run_reindex_background():
    global is_busy
    is_busy = True
    try:
        logger.info("Starte Re-Indexing...")
        indexer.rebuild_index(base_dir)
    except Exception as e:
        logger.error(f"Reindex Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def run_manual_compression_background(filename):
    global is_busy
    is_busy = True
    try:
        logger.info(f"Starte manuelle Komprimierung für {filename}...")
        path = base_dir / filename
        if path.exists():
            success = compress_pdf(path)
            if success:
                logger.info("Komprimierung erfolgreich.")
            else:
                logger.info("Komprimierung brachte keine Verbesserung.")
        else:
            logger.error("Datei nicht gefunden.")
    except Exception as e:
        logger.error(f"Komprimierung Fehler: {e}")
    finally:
        is_busy = False
        process_lock.release()


def try_start_process(target_func, *args):
    if process_lock.acquire(blocking=False):
        thread = threading.Thread(target=target_func, args=args)
        thread.start()
        return True
    return False


# --- SCHEDULER (MIT LOCK FÜR GUNICORN) ---
def job_download():
    logger.info("⏰ 06:00 - Auto-Download gestartet")
    try_start_process(run_scraper_background)


def job_reindex():
    logger.info("⏰ 06:15 - Auto-Reindex gestartet")
    try_start_process(run_reindex_background)


def start_scheduler():
    """Startet den Scheduler nur EINMAL, auch bei mehreren Workern"""
    try:
        # Wir versuchen eine Lock-Datei zu erstellen und exklusiv zu sperren
        # O_CREAT | O_WRONLY sorgt dafür, dass die Datei erstellt wird, falls nicht da
        lock_file = open("scheduler.lock", "w")

        # LOCK_EX: Exklusiver Lock
        # LOCK_NB: Non-Blocking (wirft Fehler, wenn schon gesperrt)
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Wenn wir hier sind, haben wir den Lock!
        scheduler = BackgroundScheduler()
        scheduler.add_job(func=job_download, trigger="cron", hour=6, minute=0)
        scheduler.add_job(func=job_reindex, trigger="cron", hour=6, minute=15)
        scheduler.start()

        logger.info("✅ Scheduler erfolgreich in diesem Worker gestartet (Lock erhalten).")

        # Beim Beenden des Programms Lock freigeben
        def unlock():
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()

        atexit.register(unlock)

    except IOError:
        # Lock konnte nicht erhalten werden -> Scheduler läuft schon woanders
        logger.info("ℹ️ Scheduler läuft bereits in einem anderen Worker. Überspringe.")


# Scheduler initialisieren
start_scheduler()


# --- ROUTEN ---

@app.route('/')
def index():
    query = request.args.get('q', '').strip()

    current_iso = datetime.now().isocalendar()
    current_week_id = f"{current_iso.year}-W{current_iso.week:02d}"

    selected_week = request.args.get('week', current_week_id)

    files = []
    available_weeks = set()

    if query:
        files = indexer.search_articles(query)
        flash(f'{len(files)} Treffer für "{query}" gefunden.', 'info')
    else:
        all_files = indexer.get_all_files(base_dir)
        for f in all_files:
            available_weeks.add(f['week_id'])
        files = [f for f in all_files if f['week_id'] == selected_week]

    sorted_weeks = sorted(list(available_weeks), reverse=True)

    try:
        y, w = map(int, selected_week.split('-W'))
        d = datetime.fromisocalendar(y, w, 1)
        prev_d = d - timedelta(days=7)
        next_d = d + timedelta(days=7)
        prev_week_id = f"{prev_d.isocalendar().year}-W{prev_d.isocalendar().week:02d}"
        next_week_id = f"{next_d.isocalendar().year}-W{next_d.isocalendar().week:02d}"
    except:
        prev_week_id = None
        next_week_id = None

    return render_template('index.html',
                           files=files,
                           query=query,
                           is_scraping=is_busy,
                           admin_user=is_admin(),
                           selected_week=selected_week,
                           available_weeks=sorted_weeks,
                           prev_week=prev_week_id,
                           next_week=next_week_id)


@app.route('/download/<filename>')
def download_file(filename):
    force_download = request.args.get('dl') == '1'
    response = send_from_directory(base_dir, filename, as_attachment=force_download)
    if not force_download:
        response.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


@app.route('/trigger-scrape')
def trigger_scrape():
    if not is_admin(): return redirect(url_for('index'))
    if try_start_process(run_scraper_background):
        flash('Download gestartet.', 'info')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/reindex')
def reindex():
    if not is_admin(): return redirect(url_for('index'))
    if try_start_process(run_reindex_background):
        flash('Re-Indexing gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/archive-download', methods=['POST'])
def archive_download():
    if not is_admin(): return redirect(url_for('index'))
    date_str = request.form.get('date')
    range_val = int(request.form.get('range', 1))
    if try_start_process(run_archive_background, date_str, range_val):
        flash(f'Archiv-Download gestartet.', 'success')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


@app.route('/compress/<filename>')
def compress_file_route(filename):
    if not is_admin(): return redirect(url_for('index'))
    if try_start_process(run_manual_compression_background, filename):
        flash(f'Komprimierung für {filename} gestartet. Prüfe Logs für Ergebnis.', 'info')
    else:
        flash('System beschäftigt.', 'warning')
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)