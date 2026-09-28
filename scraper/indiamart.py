"""IndiaMART scraper using Selenium (B2B suppliers/manufacturers)."""
import re
import time
import threading
from urllib.parse import quote_plus
from django.utils import timezone

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from .models import ScrapeJob, Place


def _build_url(search_term):
    """https://dir.indiamart.com/search.mp?ss=<term>"""
    return f"https://dir.indiamart.com/search.mp?ss={quote_plus(search_term.strip())}"


def run_indiamart(job_id, headless=True, max_results=100):
    job = ScrapeJob.objects.get(id=job_id)
    job.status = 'running'
    job.source = 'indiamart'
    job.save(update_fields=['status', 'source'])

    driver = None
    try:
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        wait = WebDriverWait(driver, 30)

        url = _build_url(job.search_term)
        driver.get(url)
        time.sleep(5)

        # close any popup
        for xp in ["//button[contains(.,'Close')]", "//div[@class='cls']", "//span[@id='wgt-close']"]:
            try:
                driver.find_element(By.XPATH, xp).click()
                break
            except Exception:
                pass

        # scroll to load more
        prev = 0
        for _ in range(15):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            cards = driver.find_elements(By.XPATH, "//div[contains(@class,'lst') and contains(@class,'cardlinks')]")
            if not cards:
                cards = driver.find_elements(By.XPATH, "//div[contains(@class,'card')]//div[contains(@class,'producttitle') or contains(@class,'companyname')]/ancestor::div[contains(@class,'card')]")
            if not cards:
                cards = driver.find_elements(By.XPATH, "//div[contains(@class,'lst')]")
            if len(cards) == prev or len(cards) >= max_results:
                break
            prev = len(cards)

        cards = driver.find_elements(By.XPATH, "//div[contains(@class,'lst') and contains(@class,'cardlinks')]")
        if not cards:
            cards = driver.find_elements(By.XPATH, "//div[contains(@class,'lst')]")

        saved = 0
        seen = set()

        for card in cards[:max_results]:
            try:
                try:
                    name = card.find_element(
                        By.XPATH, ".//div[contains(@class,'companyname')] | .//*[contains(@class,'producttitle')] | .//h2 | .//a[contains(@class,'cardlinks')]"
                    ).text.strip()
                except Exception:
                    name = ''
                if not name:
                    continue

                try:
                    address = card.find_element(
                        By.XPATH, ".//*[contains(@class,'newLocationUi') or contains(@class,'sloc') or contains(@class,'companyAddr')]"
                    ).text.strip()
                except Exception:
                    address = ''

                try:
                    phone_el = card.find_element(
                        By.XPATH, ".//*[contains(@class,'contactnumber') or contains(@class,'pns_h') or contains(text(),'+91')]"
                    )
                    phone = phone_el.text.strip()
                    phone = re.sub(r'[^\d+]', '', phone)[:20]
                except Exception:
                    phone = ''

                try:
                    rating = card.find_element(By.XPATH, ".//*[contains(@class,'tcw_rate') or contains(@class,'rating')]").text.strip()
                except Exception:
                    rating = ''

                try:
                    website = card.find_element(By.XPATH, ".//a[contains(@href,'http')]").get_attribute('href')
                    if 'indiamart.com' in website:
                        website = ''
                except Exception:
                    website = ''

                key = f"{name}|{address}|{phone}".lower().strip()
                if key in seen:
                    continue
                seen.add(key)

                from .services import smart_upsert_place
                smart_upsert_place(job, key, {
                    'name': name[:500],
                    'rating': rating[:20],
                    'category': job.search_term[:200],
                    'address': address,
                    'phone': phone,
                    'website': website[:500] if website else '',
                    'source': 'indiamart',
                })
                saved += 1
            except Exception:
                continue

        job.status = 'completed'
        job.total_results = saved
        job.finished_at = timezone.now()
        job.save()

    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)[:1000]
        job.finished_at = timezone.now()
        job.save()
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def start_indiamart_async(search_term, headless=True):
    job = ScrapeJob.objects.create(
        search_term=search_term,
        source='indiamart',
        status='pending',
    )
    t = threading.Thread(target=run_indiamart, args=(job.id, headless, 100), daemon=True)
    t.start()
    return job
