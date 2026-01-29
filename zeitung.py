import os
import time
import logging
import shutil
import subprocess
import re
from datetime import datetime, timedelta
from pathlib import Path

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common import exceptions
from discord_webhook import DiscordWebhook
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

IS_DOCKER = os.getenv("RUNNING_IN_DOCKER", "False").lower() == "true"

if IS_DOCKER:
    base_dir = Path("/app/downloads")
else:
    base_dir = Path(os.getcwd()) / "downloads"
base_dir.mkdir(parents=True, exist_ok=True)

SITE_CONFIG = {
    "url": os.getenv("PAPER_URL", "https://vrm-epaper.de/dashboard.act?region=E120"),
    "selectors": {
        "login_link": (By.LINK_TEXT, 'Anmelden'),
        "cookie_accept_btn": (By.XPATH,
                              "//a[contains(@class, 'cmpboxbtnyes') or contains(text(), 'Zustimmen') or contains(text(), 'Akzeptieren')]"),
        "username_field": (By.ID, 'email'),
        "password_field": (By.ID, 'password'),
        "login_submit_btn": (By.CSS_SELECTOR, "button[type='submit']"),
        "download_btn": (By.CLASS_NAME, 'pdf-download'),
        "logout_btn": (By.CSS_SELECTOR, "a[title='Abmelden']")
    },
    "credentials": {
        "user": os.getenv("PAPER_USER"),
        "pass": os.getenv("PAPER_PASS")
    },
    "proxy": os.getenv("PROXY_SERVER")
}
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")


class ZeitungScraper:
    def __init__(self):
        self.driver = None
        self.wait = None
        self.current_target_date_str = datetime.today().strftime('%Y-%m-%d')
        self.target_path = None

    def get_docker_chrome_version(self):
        try:
            result = subprocess.run(['google-chrome', '--version'], capture_output=True, text=True)
            output = result.stdout.strip()
            match = re.search(r'(\d+)', output)
            if match: return int(match.group(1))
        except:
            pass
        return None

    def setup_driver(self):
        options = uc.ChromeOptions()
        target_version = None

        if IS_DOCKER:
            options.add_argument('--headless=new')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            target_version = self.get_docker_chrome_version()
        else:
            target_version = 131

        options.add_argument('--disable-extensions')
        options.add_argument('--window-size=1920,1080')
        if SITE_CONFIG["proxy"]:
            options.add_argument(f'--proxy-server={SITE_CONFIG["proxy"]}')

        prefs = {
            'download.default_directory': str(base_dir.absolute()),
            'download.prompt_for_download': False,
            'download.directory_upgrade': True,
            'safebrowsing.enabled': True,
            'plugins.always_open_pdf_externally': True
        }
        options.add_experimental_option('prefs', prefs)

        kwargs = {'options': options}
        if target_version: kwargs['version_main'] = target_version

        self.driver = uc.Chrome(**kwargs)
        self.wait = WebDriverWait(self.driver, 30)

    def login(self):
        s = SITE_CONFIG["selectors"]
        c = SITE_CONFIG["credentials"]

        self.driver.get(SITE_CONFIG["url"])

        try:
            WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable(s["cookie_accept_btn"])).click()
            time.sleep(1)
        except:
            pass

        try:
            WebDriverWait(self.driver, 3).until(EC.presence_of_element_located(s["logout_btn"]))
            logger.info("Bereits eingeloggt.")
            return
        except:
            pass

        self.driver.execute_script("arguments[0].click();", self.driver.find_element(*s["login_link"]))
        self.wait.until(EC.url_contains("sso"))

        user_field = self.wait.until(EC.visibility_of_element_located(s["username_field"]))
        user_field.clear()
        user_field.send_keys(c["user"])

        pass_field = self.driver.find_element(*s["password_field"])
        pass_field.clear()
        pass_field.send_keys(c["pass"])

        self.driver.execute_script("arguments[0].click();", self.driver.find_element(*s["login_submit_btn"]))

        self.wait.until(EC.url_contains("vrm-epaper.de"))

    def logout(self):
        try:
            logger.info("Navigiere zum Dashboard für Logout...")
            self.driver.get(SITE_CONFIG["url"])
            time.sleep(2)

            logger.info("Führe Logout durch...")
            s = SITE_CONFIG["selectors"]

            logout_btn = self.wait.until(EC.presence_of_element_located(s["logout_btn"]))
            self.driver.execute_script("arguments[0].click();", logout_btn)

            time.sleep(2)
            logger.info("Logout erfolgreich.")
        except Exception as e:
            logger.warning(f"Logout nicht möglich: {e}")

    def clean_target_if_broken(self, target_path):
        """Löscht Datei, falls sie existiert aber leer/zu klein ist."""
        if target_path.exists():
            # Alles unter 10KB ist verdächtig für eine Zeitung (meist > 10MB)
            if target_path.stat().st_size < 10 * 1024:
                logger.warning(f"Datei {target_path.name} existiert, ist aber defekt (<10KB). Lösche sie.")
                try:
                    os.remove(target_path)
                    return False  # Existiert nicht mehr -> Neu laden
                except OSError as e:
                    logger.error(f"Konnte defekte Datei nicht löschen: {e}")
                    return True  # Wir können nichts tun, also überspringen wir lieber
            else:
                return True  # Datei existiert und ist groß genug
        return False  # Existiert nicht

    def wait_for_download(self, filename_to_save):
        logger.info(f"Warte auf Download für: {filename_to_save}")

        # Cleanup: Lösche alte .crdownload Leichen
        for temp in base_dir.glob("*.crdownload"):
            try:
                os.remove(temp)
            except:
                pass

        end_time = time.time() + 120  # Timeout erhöht auf 120s
        target_file = base_dir / filename_to_save

        while time.time() < end_time:
            files = list(base_dir.glob("*.pdf"))
            temp_files = list(base_dir.glob("*.crdownload"))

            # Logik:
            # 1. Keine Temp Dateien mehr
            # 2. Mindestens eine PDF da
            if files and not temp_files:
                latest_file = max(files, key=os.path.getctime)

                # Sicherheitscheck 1: Ist die Datei zu alt? (Älter als 2 Min -> ignorieren)
                if time.time() - os.path.getctime(latest_file) > 120:
                    time.sleep(1)
                    continue

                # Sicherheitscheck 2: HAT DIE DATEI INHALT?
                try:
                    if latest_file.stat().st_size == 0:
                        # logger.debug("Datei hat noch 0 Bytes...")
                        time.sleep(0.5)
                        continue
                except OSError:
                    continue  # Datei evtl. gerade gesperrt

                # Umbenennung
                if latest_file.name != filename_to_save:
                    if target_file.exists():
                        try:
                            os.remove(target_file)
                        except:
                            pass

                    try:
                        shutil.move(str(latest_file), str(target_file))
                        logger.info(f"Gespeichert als: {filename_to_save} (Größe: {target_file.stat().st_size} Bytes)")
                        return target_file
                    except Exception as e:
                        logger.error(f"Fehler beim Umbenennen: {e}")
                        return None
                else:
                    logger.info(f"Download fertig: {filename_to_save} (Größe: {latest_file.stat().st_size} Bytes)")
                    return target_file

            time.sleep(1)

        logger.warning(f"Timeout! Datei nicht erhalten oder leer: {filename_to_save}")
        return None

    def handle_tabs(self):
        try:
            if len(self.driver.window_handles) > 1:
                logger.info("Neuer Tab erkannt. Schließe ihn...")
                main_window = self.driver.window_handles[0]
                new_window = self.driver.window_handles[1]

                self.driver.switch_to.window(new_window)
                time.sleep(1)
                self.driver.close()

                self.driver.switch_to.window(main_window)
        except Exception as e:
            logger.warning(f"Tab Handling Fehler: {e}")

    def run_daily(self):
        try:
            self.setup_driver()
            self.login()

            s = SITE_CONFIG["selectors"]
            download_btn = self.wait.until(EC.element_to_be_clickable(s["download_btn"]))
            self.driver.execute_script("arguments[0].scrollIntoView();", download_btn)

            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"
            target_path = base_dir / filename

            # Prüfen ob wir überhaupt laden müssen
            if self.clean_target_if_broken(target_path):
                logger.info("Datei existiert bereits und ist valide. Überspringe.")
                self.logout()
                return

            download_btn.click()
            time.sleep(2)
            self.handle_tabs()

            saved_path = self.wait_for_download(filename)
            if saved_path:
                self.target_path = saved_path

            self.logout()

        except Exception as e:
            logger.error(f"Daily Error: {e}")
        finally:
            if self.driver: self.driver.quit()

    def run_archive(self, start_date_str, days_range):
        downloaded_files = []
        try:
            self.setup_driver()
            self.login()

            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")

            for i in range(days_range):
                current_date = start_date - timedelta(days=i)
                date_str_iso = current_date.strftime("%Y-%m-%d")

                target_filename = f"{date_str_iso}_Wormser_Zeitung.pdf"
                target_path = base_dir / target_filename

                # Check ob existiert + Check ob kaputt (0 Byte)
                if self.clean_target_if_broken(target_path):
                    logger.info(f"Überspringe {date_str_iso}, existiert bereits (Valide).")
                    continue

                url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={date_str_iso}&widgetId=1020&region=E120"
                logger.info(f"Navigiere zu {date_str_iso}...")
                self.driver.get(url)
                time.sleep(2)

                css_selector = f".pdf-date-{date_str_iso}"

                try:
                    container = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                    link = container.find_element(By.TAG_NAME, "a")

                    logger.info(f"Klicke Download für {date_str_iso}...")
                    self.driver.execute_script("arguments[0].click();", link)

                    time.sleep(3)
                    self.handle_tabs()

                    res = self.wait_for_download(target_filename)
                    if res:
                        downloaded_files.append(res)
                    else:
                        logger.error(f"Download für {date_str_iso} fehlgeschlagen.")

                except exceptions.NoSuchElementException:
                    logger.warning(f"Keine Ausgabe für {date_str_iso} gefunden.")
                except Exception as e:
                    logger.error(f"Fehler bei {date_str_iso}: {e}")

            self.logout()

        except Exception as e:
            logger.error(f"Archiv Error: {e}")
        finally:
            if self.driver: self.driver.quit()

        return downloaded_files

    def run(self):
        self.run_daily()