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

        # Cookie Banner aggressiver behandeln
        try:
            cookie_btn = WebDriverWait(self.driver, 5).until(EC.element_to_be_clickable(s["cookie_accept_btn"]))
            self.driver.execute_script("arguments[0].click();", cookie_btn)
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

    def cleanup_zero_byte_files(self):
        """Löscht ALLE 0KB PDF-Dateien im Ordner."""
        try:
            for f in base_dir.glob("*.pdf"):
                if f.stat().st_size == 0:
                    logger.warning(f"Cleanup: Lösche 0KB Leiche: {f.name}")
                    os.remove(f)
            for f in base_dir.glob("*.crdownload"):
                os.remove(f)
        except Exception as e:
            logger.error(f"Cleanup Fehler: {e}")

    def get_existing_pdfs(self):
        return set(base_dir.glob("*.pdf"))

    def wait_for_download(self, filename_to_save, pre_existing_files):
        logger.info(f"Warte auf NEUEN Download für: {filename_to_save}")

        # Timeout erhöht auf 180s
        end_time = time.time() + 180
        target_file = base_dir / filename_to_save

        stuck_start_time = None
        current_candidate = None

        while time.time() < end_time:
            current_files = set(base_dir.glob("*.pdf"))
            new_files = current_files - pre_existing_files
            temp_files = list(base_dir.glob("*.crdownload"))

            if new_files:
                candidate = list(new_files)[0]

                # Wenn sich der Kandidat ändert (z.B. Chrome benennt um), Timer resetten
                if candidate != current_candidate:
                    current_candidate = candidate
                    stuck_start_time = None

                # Check 1: Noch im Download?
                if any(t.name.startswith(candidate.name) for t in temp_files):
                    time.sleep(1)
                    continue

                # Check 2: 0 Byte Deadlock (Das ist dein Problemfall!)
                try:
                    current_size = candidate.stat().st_size
                    if current_size == 0:
                        if stuck_start_time is None:
                            stuck_start_time = time.time()

                        elapsed = time.time() - stuck_start_time
                        if elapsed > 20:  # Nach 20 Sekunden 0 Bytes -> Kill
                            logger.error(
                                f"DATEI HÄNGT (0 Bytes seit {int(elapsed)}s): {candidate.name}. Lösche und breche ab.")
                            try:
                                os.remove(candidate)
                            except:
                                pass
                            return None  # Löst Retry aus

                        if int(elapsed) % 5 == 0:  # Alle 5 sek loggen
                            logger.info(f"Warte auf Daten... {candidate.name} hat 0 Bytes (seit {int(elapsed)}s)")

                        time.sleep(1)
                        continue
                    else:
                        stuck_start_time = None  # Datei hat Inhalt
                except OSError:
                    continue

                    # Check 3: Dateigröße stabil?
                try:
                    initial_size = candidate.stat().st_size
                    time.sleep(2)
                    if candidate.stat().st_size != initial_size:
                        continue  # Wächst noch
                except:
                    continue

                # ALLES OK -> Umbenennen
                logger.info(f"Download fertig erkannt: {candidate.name} ({initial_size} Bytes)")

                if candidate.name != filename_to_save:
                    if target_file.exists():
                        try:
                            os.remove(target_file)
                        except:
                            pass
                    try:
                        shutil.move(str(candidate), str(target_file))
                        logger.info(f"Gespeichert als: {filename_to_save}")
                        return target_file
                    except Exception as e:
                        logger.error(f"Fehler beim Umbenennen: {e}")
                        return None
                else:
                    logger.info(f"Datei hat bereits korrekten Namen: {filename_to_save}")
                    return target_file

            time.sleep(1)

        logger.warning(f"Timeout! Keine NEUE, valide Datei erhalten für: {filename_to_save}")
        return None

    def handle_tabs(self):
        try:
            if len(self.driver.window_handles) > 1:
                main_window = self.driver.window_handles[0]
                new_window = self.driver.window_handles[1]
                self.driver.switch_to.window(new_window)
                time.sleep(1)
                self.driver.close()
                self.driver.switch_to.window(main_window)
        except:
            pass

    def run_daily(self):
        try:
            self.setup_driver()
            self.login()

            s = SITE_CONFIG["selectors"]
            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"
            target_path = base_dir / filename

            # Existenz-Check (Muss > 1KB sein)
            if target_path.exists() and target_path.stat().st_size > 1024:
                logger.info("Datei existiert bereits und ist valide. Überspringe.")
                self.logout()
                return

            for attempt in range(1, 4):
                try:
                    logger.info(f"Versuch {attempt}/3 für Daily Download...")
                    self.cleanup_zero_byte_files()  # WICHTIG: Leichen weg!

                    if attempt > 1:
                        self.driver.refresh()
                        time.sleep(5)

                    download_btn = WebDriverWait(self.driver, 20).until(
                        EC.element_to_be_clickable(s["download_btn"])
                    )
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_btn)
                    time.sleep(1)

                    known_files = self.get_existing_pdfs()
                    self.driver.execute_script("arguments[0].click();", download_btn)

                    time.sleep(2)
                    self.handle_tabs()

                    saved_path = self.wait_for_download(filename, known_files)
                    if saved_path:
                        self.target_path = saved_path
                        break
                    else:
                        logger.warning(f"Versuch {attempt} fehlgeschlagen.")

                except Exception as e:
                    logger.error(f"Fehler bei Versuch {attempt}: {e}")
                    time.sleep(5)

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

                if target_path.exists() and target_path.stat().st_size > 1024:
                    logger.info(f"Überspringe {date_str_iso}, existiert bereits (Valide).")
                    continue

                for attempt in range(1, 4):
                    logger.info(f"Versuch {attempt}/3 für {date_str_iso}...")
                    self.cleanup_zero_byte_files()

                    try:
                        url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={date_str_iso}&widgetId=1020&region=E120"
                        self.driver.get(url)
                        time.sleep(4)

                        css_selector = f".pdf-date-{date_str_iso}"

                        try:
                            container = WebDriverWait(self.driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, css_selector))
                            )
                            link = container.find_element(By.TAG_NAME, "a")

                            known_files = self.get_existing_pdfs()
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                            time.sleep(1)
                            self.driver.execute_script("arguments[0].click();", link)

                            time.sleep(3)
                            self.handle_tabs()

                            res = self.wait_for_download(target_filename, known_files)
                            if res:
                                downloaded_files.append(res)
                                time.sleep(1)
                                break
                            else:
                                logger.warning(f"Download fehlgeschlagen für {date_str_iso}.")

                        except exceptions.NoSuchElementException:
                            logger.warning(f"Keine Ausgabe für {date_str_iso} gefunden.")
                            break
                        except exceptions.TimeoutException:
                            logger.warning(f"Element nicht gefunden (Timeout) für {date_str_iso}.")

                    except Exception as e:
                        logger.error(f"Fehler bei {date_str_iso} (Versuch {attempt}): {e}")
                        time.sleep(5)

            self.logout()

        except Exception as e:
            logger.error(f"Archiv Error: {e}")
        finally:
            if self.driver: self.driver.quit()

        return downloaded_files

    def run(self):
        self.run_daily()