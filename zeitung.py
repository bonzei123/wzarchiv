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

# NEU: Importiere den Kompressor für die PDF-Optimierung
try:
    from compressor import compress_pdf
except ImportError:
    # Fallback, falls die Datei lokal beim Testen fehlt
    def compress_pdf(path):
        return False

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
            target_version = 131  # Hier ggf. deine lokale Version anpassen

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
            if target_path.stat().st_size < 10 * 1024:
                logger.warning(f"Datei {target_path.name} existiert, ist aber defekt (<10KB). Lösche sie.")
                try:
                    os.remove(target_path)
                    return False
                except OSError as e:
                    logger.error(f"Konnte defekte Datei nicht löschen: {e}")
                    return True
            else:
                return True
        return False

    def get_existing_pdfs(self):
        """Erstellt einen Snapshot der aktuellen Dateien im Ordner"""
        return set(base_dir.glob("*.pdf"))

    def wait_for_download(self, filename_to_save, pre_existing_files):
        """Wartet auf eine NEUE Datei, die nicht im Snapshot war"""
        logger.info(f"Warte auf NEUEN Download für: {filename_to_save}")

        for temp in base_dir.glob("*.crdownload"):
            try:
                os.remove(temp)
            except:
                pass

        end_time = time.time() + 180
        target_file = base_dir / filename_to_save

        while time.time() < end_time:
            current_files = set(base_dir.glob("*.pdf"))
            new_files = current_files - pre_existing_files
            temp_files = list(base_dir.glob("*.crdownload"))

            if new_files:
                candidate = list(new_files)[0]

                # Check 1: Noch im Download?
                if any(t.name.startswith(candidate.name) for t in temp_files):
                    time.sleep(1)
                    continue

                # Check 2: 0 Byte Deadlock?
                try:
                    if candidate.stat().st_size == 0:
                        time.sleep(1)
                        continue
                except OSError:
                    continue

                    # Check 3: Dateigröße stabil?
                try:
                    initial_size = candidate.stat().st_size
                    time.sleep(2)
                    if candidate.stat().st_size != initial_size:
                        continue
                except:
                    continue

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

                        # --- KOMPRIMIERUNG ---
                        compress_pdf(target_file)
                        # ---------------------

                        return target_file
                    except Exception as e:
                        logger.error(f"Fehler beim Umbenennen: {e}")
                        return None
                else:
                    # Auch hier komprimieren, falls der Name zufällig schon stimmte
                    compress_pdf(target_file)
                    return target_file

            time.sleep(1)

        logger.warning(f"Timeout! Keine NEUE, valide Datei erhalten für: {filename_to_save}")
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

            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"
            target_path = base_dir / filename

            if self.clean_target_if_broken(target_path):
                logger.info("Datei existiert bereits und ist valide. Überspringe.")
                self.logout()
                return

            # RETRY LOGIK
            for attempt in range(1, 4):
                try:
                    logger.info(f"Versuch {attempt}/3 für Daily Download...")
                    s = SITE_CONFIG["selectors"]

                    if attempt > 1:
                        self.driver.refresh()
                        time.sleep(3)

                    download_btn = self.wait.until(EC.element_to_be_clickable(s["download_btn"]))
                    self.driver.execute_script("arguments[0].scrollIntoView();", download_btn)

                    known_files = self.get_existing_pdfs()
                    download_btn.click()
                    time.sleep(2)
                    self.handle_tabs()

                    saved_path = self.wait_for_download(filename, known_files)
                    if saved_path:
                        self.target_path = saved_path
                        break
                    else:
                        logger.warning(f"Versuch {attempt} fehlgeschlagen (Timeout).")

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

                if self.clean_target_if_broken(target_path):
                    logger.info(f"Überspringe {date_str_iso}, existiert bereits (Valide).")
                    continue

                for attempt in range(1, 4):
                    logger.info(f"Versuch {attempt}/3 für {date_str_iso}...")

                    try:
                        url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={date_str_iso}&widgetId=1020&region=E120"
                        self.driver.get(url)
                        time.sleep(3)

                        css_selector = f".pdf-date-{date_str_iso}"

                        try:
                            container = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                            link = container.find_element(By.TAG_NAME, "a")

                            known_files = self.get_existing_pdfs()
                            logger.info(f"Klicke Download...")
                            self.driver.execute_script("arguments[0].click();", link)

                            time.sleep(3)
                            self.handle_tabs()

                            res = self.wait_for_download(target_filename, known_files)
                            if res:
                                downloaded_files.append(res)
                                time.sleep(1)
                                break
                            else:
                                logger.warning(f"Download Timeout für {date_str_iso}.")
                                if target_path.exists() and target_path.stat().st_size == 0:
                                    os.remove(target_path)

                        except exceptions.NoSuchElementException:
                            logger.warning(f"Keine Ausgabe für {date_str_iso} gefunden.")
                            break

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