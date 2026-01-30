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
                              "//a[contains(@class, 'cmpboxbtnyes') or contains(text(), 'Zustimmen') or contains(text(), 'Akzeptieren') or contains(text(), 'Alles akzeptieren')]"),
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
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None

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

        logger.info("Starte Chrome Driver...")
        self.driver = uc.Chrome(**kwargs)
        self.wait = WebDriverWait(self.driver, 45)

    def login(self):
        s = SITE_CONFIG["selectors"]
        c = SITE_CONFIG["credentials"]

        # Geht exakt auf die Dashboard URL aus der Config
        logger.info(f"Lade Seite: {SITE_CONFIG['url']}")
        self.driver.get(SITE_CONFIG["url"])

        # Cookie Banner
        try:
            time.sleep(2)
            cookie_btn = self.driver.find_element(*s["cookie_accept_btn"])
            self.driver.execute_script("arguments[0].click();", cookie_btn)
            time.sleep(1)
        except Exception:
            pass

        # Bereits eingeloggt?
        try:
            if len(self.driver.find_elements(*s["logout_btn"])) > 0:
                logger.info("Bereits eingeloggt.")
                return
        except:
            pass

        # Login
        logger.info("Logge ein...")
        login_link = self.wait.until(EC.element_to_be_clickable(s["login_link"]))
        self.driver.execute_script("arguments[0].click();", login_link)

        self.wait.until(EC.url_contains("sso"))

        user_field = self.wait.until(EC.visibility_of_element_located(s["username_field"]))
        user_field.clear()
        user_field.send_keys(c["user"])

        pass_field = self.driver.find_element(*s["password_field"])
        pass_field.clear()
        pass_field.send_keys(c["pass"])

        submit = self.driver.find_element(*s["login_submit_btn"])
        self.driver.execute_script("arguments[0].click();", submit)

        self.wait.until(EC.url_contains("vrm-epaper.de"))
        logger.info("Login erfolgreich.")

    def logout(self):
        try:
            logger.info("Logout...")
            self.driver.get(SITE_CONFIG["url"])
            time.sleep(2)
            s = SITE_CONFIG["selectors"]
            logout_btn = self.wait.until(EC.presence_of_element_located(s["logout_btn"]))
            self.driver.execute_script("arguments[0].click();", logout_btn)
            time.sleep(2)
            logger.info("Logout erfolgreich.")
        except:
            pass

    # --- DIE ALTE, EINFACHE LOGIK FÜR HEUTE ---
    def wait_simple(self, filename_to_save):
        logger.info(f"Warte auf Datei (Simple Mode): {filename_to_save}")
        end_time = time.time() + 180
        target_file = base_dir / filename_to_save

        while time.time() < end_time:
            files = list(base_dir.glob("*.pdf"))
            temp_files = list(base_dir.glob("*.crdownload"))

            # Wenn PDF da ist und KEIN Download mehr läuft -> Neueste nehmen
            if files and not temp_files:
                latest_file = max(files, key=os.path.getctime)

                # Check: Ist die Datei valide? (>0 Byte)
                try:
                    if latest_file.stat().st_size > 0:
                        if latest_file.name != filename_to_save:
                            if target_file.exists():
                                os.remove(target_file)
                            shutil.move(str(latest_file), str(target_file))
                            logger.info(f"Gespeichert als: {filename_to_save}")
                            return target_file
                        else:
                            return target_file
                except:
                    pass

            time.sleep(1)

        logger.error("Timeout: Keine Datei erhalten.")
        return None

    def run_daily(self):
        """
        Originaler Ablauf: Dashboard URL -> Login -> Klick -> Warten -> Logout.
        """
        try:
            self.setup_driver()
            self.login()

            s = SITE_CONFIG["selectors"]
            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"
            target_path = base_dir / filename

            if target_path.exists() and target_path.stat().st_size > 1024:
                logger.info("Datei existiert bereits. Fertig.")
                return

            # Button suchen & Klicken
            download_btn = WebDriverWait(self.driver, 20).until(
                EC.element_to_be_clickable(s["download_btn"])
            )
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_btn)
            time.sleep(1)

            logger.info("Klicke Download Button...")
            self.driver.execute_script("arguments[0].click();", download_btn)

            # Warten (Simple Logic)
            saved_path = self.wait_simple(filename)

            if saved_path:
                self.target_path = saved_path

            self.logout()

        except Exception as e:
            logger.error(f"Daily Error: {e}")
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass

    # --- ARCHIV FUNKTION (Getrennt) ---
    def get_existing_pdfs(self):
        return set(base_dir.glob("*.pdf"))

    def wait_for_download_archive(self, filename_to_save, pre_existing_files):
        end_time = time.time() + 180
        target_file = base_dir / filename_to_save

        while time.time() < end_time:
            current_files = set(base_dir.glob("*.pdf"))
            new_files = current_files - pre_existing_files
            temp_files = list(base_dir.glob("*.crdownload"))

            if new_files:
                candidate = list(new_files)[0]
                if not any(t.name.startswith(candidate.name) for t in temp_files):
                    try:
                        if candidate.stat().st_size > 0:
                            if candidate.name != filename_to_save:
                                if target_file.exists(): os.remove(target_file)
                                shutil.move(str(candidate), str(target_file))
                            return target_file
                    except:
                        pass
            time.sleep(1)
        return None

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
                    continue

                try:
                    url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={date_str_iso}&widgetId=1020&region=E120"
                    self.driver.get(url)
                    time.sleep(3)

                    css_selector = f".pdf-date-{date_str_iso}"
                    container = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                    link = container.find_element(By.TAG_NAME, "a")

                    known_files = self.get_existing_pdfs()
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
                    self.driver.execute_script("arguments[0].click();", link)

                    res = self.wait_for_download_archive(target_filename, known_files)
                    if res: downloaded_files.append(res)
                    time.sleep(2)

                except Exception as e:
                    logger.error(f"Fehler Archiv {date_str_iso}: {e}")

            self.logout()
        except Exception as e:
            logger.error(f"Archiv Error: {e}")
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
        return downloaded_files

    def run(self):
        self.run_daily()