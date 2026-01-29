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
            # options.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
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
            'plugins.always_open_pdf_externally': True  # PDF Download erzwingen statt Viewer
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
            # WICHTIG: Zurück zum Dashboard, da ist der Logout Button sicher
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

    def wait_for_download(self, filename_to_save):
        logger.info(f"Warte auf Download für: {filename_to_save}")

        # Cleanup: Lösche alte .crdownload Leichen, damit wir uns nicht verwirren
        for temp in base_dir.glob("*.crdownload"):
            try:
                os.remove(temp)
            except:
                pass

        end_time = time.time() + 90  # Mehr Zeit geben (90s)
        target_file = base_dir / filename_to_save

        # Warte, bis überhaupt eine neue Datei auftaucht
        while time.time() < end_time:
            files = list(base_dir.glob("*.pdf"))
            temp_files = list(base_dir.glob("*.crdownload"))

            # Logik: Wir warten bis KEINE .crdownload mehr da ist, UND eine PDF da ist.
            if files and not temp_files:
                # Wir nehmen die allerneueste Datei
                latest_file = max(files, key=os.path.getctime)

                # Sicherheitscheck: Ist die Datei jünger als 2 Minuten? (Nicht dass wir eine alte indexieren)
                if time.time() - os.path.getctime(latest_file) > 120:
                    time.sleep(1)
                    continue

                if latest_file.name != filename_to_save:
                    # Falls Zieldatei schon existiert (Fehler beim letzten Mal?), löschen
                    if target_file.exists():
                        os.remove(target_file)

                    # Umbenennen
                    try:
                        shutil.move(str(latest_file), str(target_file))
                        logger.info(f"Gespeichert als: {filename_to_save}")
                        return target_file
                    except Exception as e:
                        logger.error(f"Fehler beim Umbenennen: {e}")
                        return None
                else:
                    return target_file  # Name stimmte schon

            time.sleep(1)

        logger.warning(f"Timeout! Datei nicht erhalten: {filename_to_save}")
        return None

    def handle_tabs(self):
        """Schließt neu geöffnete Tabs und kehrt zum Hauptfenster zurück"""
        try:
            if len(self.driver.window_handles) > 1:
                logger.info("Neuer Tab erkannt. Schließe ihn...")
                main_window = self.driver.window_handles[0]
                new_window = self.driver.window_handles[1]

                self.driver.switch_to.window(new_window)
                time.sleep(1)  # Kurz warten, falls Download Trigger hier liegt
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
            download_btn.click()

            # Check Tabs
            time.sleep(2)
            self.handle_tabs()

            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"

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

            # Wir iterieren durch die Tage
            for i in range(days_range):
                current_date = start_date - timedelta(days=i)
                date_str_iso = current_date.strftime("%Y-%m-%d")

                target_filename = f"{date_str_iso}_Wormser_Zeitung.pdf"
                target_path = base_dir / target_filename

                if target_path.exists():
                    logger.info(f"Überspringe {date_str_iso}, existiert bereits.")
                    continue

                # WICHTIG: Wir laden die Seite für JEDEN Download neu (oder gehen auf die URL)
                # Das verhindert, dass wir auf der falschen Seite hängen oder DOM-Elemente "stale" werden.
                # Wir rufen direkt die Widget-URL für diesen Tag auf (dateTo = Tag)
                url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={date_str_iso}&widgetId=1020&region=E120"
                logger.info(f"Navigiere zu {date_str_iso}...")
                self.driver.get(url)
                time.sleep(2)  # Seite laden lassen

                css_selector = f".pdf-date-{date_str_iso}"

                try:
                    # Suche spezifisch nach dem Container für diesen Tag
                    container = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                    link = container.find_element(By.TAG_NAME, "a")

                    logger.info(f"Klicke Download für {date_str_iso}...")

                    # Manche JS Buttons öffnen neue Fenster, manche nicht.
                    # Wir klicken und schauen was passiert.
                    self.driver.execute_script("arguments[0].click();", link)

                    # Warten falls Tab aufgeht
                    time.sleep(3)
                    self.handle_tabs()

                    # Jetzt warten wir auf genau diese Datei
                    res = self.wait_for_download(target_filename)
                    if res:
                        downloaded_files.append(res)
                    else:
                        logger.error(f"Download für {date_str_iso} fehlgeschlagen (keine Datei).")

                except exceptions.NoSuchElementException:
                    logger.warning(f"Keine Ausgabe für {date_str_iso} gefunden (evtl. Feiertag/Sonntag)")
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