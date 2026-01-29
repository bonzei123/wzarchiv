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
        # NEU: Logout Button (Selektor via Title Attribut ist sehr stabil)
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
            # Lokal (ggf anpassen)
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
            'safebrowsing.enabled': True
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
            # Check ob Logout Button da ist -> dann sind wir schon drin
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
        """Führt den Logout durch, um Sessions freizugeben"""
        try:
            logger.info("Führe Logout durch...")
            s = SITE_CONFIG["selectors"]
            # Wir warten kurz, falls noch Overlays da sind
            time.sleep(1)

            # Wir nutzen execute_script, da der Button oft in einem Dropdown versteckt ist.
            # Ein normaler .click() würde fehlschlagen, wenn das Menü nicht offen ist.
            # JS Click funktioniert auch auf versteckten Elementen.
            logout_btn = self.driver.find_element(*s["logout_btn"])
            self.driver.execute_script("arguments[0].click();", logout_btn)

            # Kurz warten bis Logout durch ist (Seite lädt neu)
            time.sleep(2)
            logger.info("Logout erfolgreich.")
        except Exception as e:
            logger.warning(f"Logout nicht möglich (vielleicht schon ausgeloggt?): {e}")

    def wait_for_download(self, filename_to_save):
        logger.info(f"Warte auf Download für: {filename_to_save}")
        end_time = time.time() + 60

        target_file = base_dir / filename_to_save

        while time.time() < end_time:
            files = list(base_dir.glob("*.pdf"))
            temp_files = list(base_dir.glob("*.crdownload"))

            if files and not temp_files:
                latest_file = max(files, key=os.path.getctime)
                if latest_file.name != filename_to_save:
                    if target_file.exists():
                        os.remove(target_file)
                    shutil.move(str(latest_file), str(target_file))
                    logger.info(f"Gespeichert als: {filename_to_save}")
                return target_file
            time.sleep(1)
        logger.warning(f"Timeout beim Download von {filename_to_save}")
        return None

    def run_daily(self):
        try:
            self.setup_driver()
            self.login()

            s = SITE_CONFIG["selectors"]
            download_btn = self.wait.until(EC.element_to_be_clickable(s["download_btn"]))
            self.driver.execute_script("arguments[0].scrollIntoView();", download_btn)
            download_btn.click()

            today_str = datetime.today().strftime('%Y-%m-%d')
            filename = f"{today_str}_Wormser_Zeitung.pdf"

            saved_path = self.wait_for_download(filename)
            if saved_path:
                self.target_path = saved_path

            # WICHTIG: Logout am Ende der Logik
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

            url = f"https://vrm-epaper.de/widgetshelf.act?dateTo={start_date_str}&widgetId=1020&region=E120"
            logger.info(f"Rufe Archiv-URL auf: {url}")
            self.driver.get(url)
            time.sleep(2)

            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")

            for i in range(days_range):
                current_date = start_date - timedelta(days=i)
                date_str_iso = current_date.strftime("%Y-%m-%d")

                target_filename = f"{date_str_iso}_Wormser_Zeitung.pdf"
                target_path = base_dir / target_filename

                if target_path.exists():
                    logger.info(f"Überspringe {date_str_iso}, existiert bereits.")
                    continue

                css_selector = f".pdf-date-{date_str_iso}"

                try:
                    container = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                    link = container.find_element(By.TAG_NAME, "a")

                    logger.info(f"Lade Ausgabe für {date_str_iso}...")
                    self.driver.execute_script("arguments[0].click();", link)

                    res = self.wait_for_download(target_filename)
                    if res:
                        downloaded_files.append(res)
                        time.sleep(2)

                except exceptions.NoSuchElementException:
                    logger.warning(f"Keine Ausgabe gefunden für {date_str_iso}")
                except Exception as e:
                    logger.error(f"Fehler bei {date_str_iso}: {e}")

            # WICHTIG: Logout am Ende der Logik
            self.logout()

        except Exception as e:
            logger.error(f"Archiv Error: {e}")
        finally:
            if self.driver: self.driver.quit()

        return downloaded_files

    def run(self):
        self.run_daily()