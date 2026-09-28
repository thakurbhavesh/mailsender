"""JustDial scraper using Selenium (Indian B2B leads platform)."""
import re
import time
import threading
from django.utils import timezone

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from .models import ScrapeJob, Place


def _build_url(search_term, city='Delhi'):
    """Build a JustDial search URL. Format: https://www.justdial.com/<City>/<Term>"""
    term = search_term.strip().replace(' ', '-')
    return f"https://www.justdial.com/{city.replace(' ', '-')}/{term}"


def run_justdial(job_id, headless=True, max_results=100, city='Delhi'):
    job = ScrapeJob.objects.get(id=job_id)
    job.status = 'running'
    job.source = 'justdial'
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

        url = _build_url(job.search_term, city)
        driver.get(url)
        time.sleep(5)

        # Scroll & load more
        prev = 0
        for _ in range(20):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            cards = driver.find_elements(By.XPATH, "//div[contains(@class,'resultbox')]")
            if not cards:
                cards = driver.find_elements(By.XPATH, "//li[contains(@class,'cntanr')]")
            if len(cards) == prev or len(cards) >= max_results:
                break
            prev = len(cards)

        cards = driver.find_elements(By.XPATH, "//div[contains(@class,'resultbox')]")
        if not cards:
            cards = driver.find_elements(By.XPATH, "//li[contains(@class,'cntanr')]")

        saved = 0
        seen = set()

        for card in cards[:max_results]:
            try:
                try:
                    name = card.find_element(By.XPATH, ".//h2 | .//*[contains(@class,'lng_cont_name')] | .//*[contains(@class,'resultbox_title')]").text.strip()
                except Exception:
                    name = ''
                if not name:
                    continue

                try:
                    rating = card.find_element(By.XPATH, ".//*[contains(@class,'star_value') or contains(@class,'green-box')]").text.strip()
                except Exception:
                    rating = ''

                try:
                    address = card.find_element(By.XPATH, ".//*[contains(@class,'address') or contains(@class,'cont_fl_addr') or contains(@class,'resultbox_address')]").text.strip()
                except Exception:
                    address = ''

                try:
                    phone_el = card.find_element(By.XPATH, ".//*[contains(@class,'mobilesv') or contains(@class,'callcontent') or contains(@class,'callNowAnchor')]")
                    phone = phone_el.text.strip() or phone_el.get_attribute('href') or ''
                    phone = re.sub(r'[^\d+]', '', phone)[:20]
                except Exception:
                    phone = ''

                try:
                    website = card.find_element(By.XPATH, ".//a[contains(@href,'http') and not(contains(@href,'justdial'))]").get_attribute('href')
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
                    'reviews_count': 0,
                    'category': job.search_term[:200],
                    'address': address,
                    'phone': phone,
                    'website': website[:500] if website else '',
                    'source': 'justdial',
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


def start_justdial_async(search_term, city='Delhi', headless=True):
    job = ScrapeJob.objects.create(
        search_term=search_term,
        source='justdial',
        status='pending',
    )
    t = threading.Thread(target=run_justdial, args=(job.id, headless, 100, city), daemon=True)
    t.start()
    return job
