"""Selenium-based Google Maps scraper integrated with Django ORM.
Robust scrolling + multi-strategy extraction to capture ALL visible results.
"""
import re
import time
import random
import threading
from django.utils import timezone

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

from django.utils import timezone as _tz
from .models import ScrapeJob, Place


def smart_upsert_place(job, unique_key, new_data):
    """Smart deduplication:
    - If Place with this unique_key exists: merge — only overwrite blank fields
      with non-blank new values. If anything actually changed, log it. Increment
      times_seen and re-link to current job (so user sees it in this scrape too).
    - If not exists: create new linked to current job.
    Returns ('created'|'updated'|'unchanged', place).
    """
    try:
        place = Place.objects.get(unique_key=unique_key)
    except Place.DoesNotExist:
        place = Place.objects.create(job=job, unique_key=unique_key, **new_data)
        return 'created', place

    changes = []
    for k, v in new_data.items():
        # Allow numeric 0/False to skip too
        if v is None or v == '':
            continue
        old = getattr(place, k, '')
        # bools and numbers — overwrite when default
        if isinstance(v, bool):
            if v != old:
                setattr(place, k, v)
                changes.append(f"{k}: {old} → {v}")
            continue
        if isinstance(v, (int, float)):
            if not old or (v and v > old):
                setattr(place, k, v)
                changes.append(f"{k}: {old} → {v}")
            continue
        # Strings: overwrite if blank or richer
        if not old:
            setattr(place, k, v)
            changes.append(f"{k}: (empty) → {str(v)[:40]}")
        elif str(old) != str(v) and len(str(v)) > len(str(old)):
            setattr(place, k, v)
            changes.append(f"{k}: {str(old)[:30]} → {str(v)[:30]}")

    place.times_seen += 1
    place.job = job
    if changes:
        place.last_change_log = " | ".join(changes)[:1000]
        place.save()
        return 'updated', place
    place.save(update_fields=['times_seen', 'job', 'updated_at'])
    return 'unchanged', place


def _slow_type(el, text):
    for c in text:
        el.send_keys(c)
        time.sleep(random.uniform(0.05, 0.1))


def _unique_key(name, address, phone):
    return f"{name}|{address}|{phone}".lower().strip()


def _parse_rating_reviews(combined):
    if not combined or combined == "N/A":
        return "", 0, ""
    m = re.match(r"([\d.]+)\s*\((\d[\d,]*)\)", combined)
    if m:
        try:
            count = int(m.group(2).replace(',', ''))
        except ValueError:
            count = 0
        return m.group(1), count, combined
    return "", 0, combined


def run_scrape(job_id, headless=True):
    """Run scraping for a ScrapeJob in a background thread.
    Updates job.total_results live as data is saved (every 5 records)."""
    job = ScrapeJob.objects.get(id=job_id)
    job.status = 'running'
    job.save(update_fields=['status'])

    driver = None
    try:
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--window-size=1920,1080")
        else:
            options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        wait = WebDriverWait(driver, 30)
        short_wait = WebDriverWait(driver, 6)

        # ─── Helpers (operate on details panel) ────────────────────
        def wait_panel():
            try:
                wait.until(EC.presence_of_element_located(
                    (By.XPATH, "//h1[contains(@class,'DUwDvf')] | //h1[contains(@class,'fontHeadlineLarge')]")
                ))
                return True
            except Exception:
                return False

        def get_name():
            for xp in [
                "//h1[contains(@class,'DUwDvf')]",
                "//h1[contains(@class,'fontHeadlineLarge')]",
                "//div[contains(@class,'fontHeadlineLarge')]",
            ]:
                try:
                    t = driver.find_element(By.XPATH, xp).text.strip()
                    if t:
                        return t
                except Exception:
                    continue
            return ""

        def get_rating_reviews():
            try:
                rating = driver.find_element(
                    By.XPATH, "//div[contains(@class,'F7nice')]//span[@aria-hidden='true']"
                ).text.strip()
                reviews_el = driver.find_element(
                    By.XPATH, "//div[contains(@class,'F7nice')]//span[contains(@aria-label,'review')]"
                )
                reviews_text = reviews_el.get_attribute("aria-label") or reviews_el.text
                m = re.search(r'([\d,]+)', reviews_text)
                rev = m.group(1).replace(',', '') if m else '0'
                return f"{rating}({rev})"
            except Exception:
                pass
            try:
                rating = driver.find_element(By.XPATH, "//span[@role='img' and contains(@aria-label,'star')]").get_attribute('aria-label')
                m = re.search(r'([\d.]+)', rating or '')
                if m:
                    return f"{m.group(1)}(0)"
            except Exception:
                pass
            return ""

        def get_category():
            for xp in [
                "//button[contains(@class,'DkEaL')]",
                "//button[@jsaction and contains(@class,'fontBodyMedium')]",
            ]:
                try:
                    t = driver.find_element(By.XPATH, xp).text.strip()
                    if t and len(t) < 100:
                        return t
                except Exception:
                    continue
            return ""

        def get_detail(label):
            for xp in [
                f"//button[contains(@aria-label,'{label}')]",
                f"//button[contains(@data-tooltip,'{label}')]",
                f"//*[@data-item-id='{label.lower()}']",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    aria = el.get_attribute("aria-label") or ""
                    if aria:
                        return aria.replace(label + ":", "").replace(label, "").strip()
                    txt = el.text.strip()
                    if txt:
                        return txt
                except Exception:
                    continue
            return ""

        def get_address():
            return get_detail("Address")

        def get_phone():
            v = get_detail("Phone")
            if v:
                return v
            try:
                el = driver.find_element(By.XPATH, "//*[contains(@aria-label,'Call')]")
                a = el.get_attribute("aria-label") or ""
                m = re.search(r'[\+\d][\d\s\-\(\)]{6,}', a)
                if m:
                    return m.group(0).strip()
            except Exception:
                pass
            return ""

        def get_website():
            for xp in [
                "//a[@data-item-id='authority']",
                "//a[contains(@aria-label,'Website')]",
                "//a[contains(@data-tooltip,'Website')]",
            ]:
                try:
                    href = driver.find_element(By.XPATH, xp).get_attribute("href")
                    if href and 'google.com/maps' not in href and 'google.com/search' not in href:
                        return href
                except Exception:
                    continue
            return ""

        def get_hours():
            """Extract opening hours table as text."""
            for xp in [
                "//div[contains(@aria-label,'Hours')]//table",
                "//table[contains(@class,'eK4R0e')]",
                "//div[@aria-label and contains(@aria-label,'open')]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    text = el.text.strip()
                    if text and len(text) < 600:
                        return text
                except Exception:
                    continue
            try:
                btn = driver.find_element(By.XPATH, "//div[contains(@aria-label,'Hours')]")
                return (btn.get_attribute('aria-label') or '')[:600]
            except Exception:
                return ""

        def get_description():
            for xp in [
                "//div[@class='PYvSYb']",
                "//div[contains(@class,'WeS02d')]//div",
                "//button[contains(@aria-label,'About')]/following::div[1]",
            ]:
                try:
                    t = driver.find_element(By.XPATH, xp).text.strip()
                    if t and 20 < len(t) < 1000:
                        return t
                except Exception:
                    continue
            return ""

        def get_plus_code():
            try:
                btn = driver.find_element(By.XPATH, "//button[contains(@aria-label,'Plus code')]")
                aria = btn.get_attribute('aria-label') or ''
                return aria.replace('Plus code:', '').strip()[:50]
            except Exception:
                return ""

        def get_services():
            """Service chips like Dine-in, Takeout, Delivery."""
            services = []
            for xp in [
                "//div[contains(@class,'LTs0Rc')]//span",
                "//div[@aria-label and contains(@aria-label,'Service options')]//*[self::span or self::div][string-length(text())>0]",
            ]:
                try:
                    els = driver.find_elements(By.XPATH, xp)
                    for el in els[:15]:
                        t = el.text.strip()
                        if t and 2 < len(t) < 40 and t not in services:
                            services.append(t)
                except Exception:
                    continue
            return ', '.join(services[:12])

        def get_secondary_categories():
            try:
                els = driver.find_elements(By.XPATH, "//button[contains(@class,'DkEaL')]")
                cats = [e.text.strip() for e in els if e.text.strip()]
                if len(cats) > 1:
                    return ', '.join(cats[1:5])
            except Exception:
                pass
            return ""

        def get_photos_count():
            try:
                btn = driver.find_element(By.XPATH, "//button[contains(@aria-label,'photos') or contains(@aria-label,'See all')]")
                aria = btn.get_attribute('aria-label') or ''
                m = re.search(r'(\d[\d,]*)', aria)
                if m:
                    return int(m.group(1).replace(',', ''))
            except Exception:
                pass
            return 0

        def get_claimed():
            try:
                driver.find_element(By.XPATH, "//*[contains(text(),'Claim this business') or contains(text(),'Own this business')]")
                return False
            except Exception:
                return True

        def get_price_level():
            try:
                el = driver.find_element(By.XPATH, "//span[contains(@aria-label,'Price') or contains(@aria-label,'price')]")
                return (el.text.strip() or el.get_attribute('aria-label') or '')[:10]
            except Exception:
                return ""

        def get_place_url():
            try:
                return driver.current_url
            except Exception:
                return ""

        def get_lat_lng():
            """Extract from current_url which contains @lat,lng,zoom."""
            try:
                url = driver.current_url
                m = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', url)
                if m:
                    return float(m.group(1)), float(m.group(2))
            except Exception:
                pass
            return None, None

        # ─── Sidebar / scroll handling ─────────────────────────────
        def get_sidebar():
            for xp in [
                "//div[@role='feed']",
                "//div[contains(@aria-label,'Results')]",
                "//div[contains(@aria-label,'Results for')]",
            ]:
                try:
                    return short_wait.until(EC.presence_of_element_located((By.XPATH, xp)))
                except Exception:
                    pass
            return None

        # ─── START ─────────────────────────────────────────────────
        driver.get("https://www.google.com/maps")
        time.sleep(4)

        try:
            short_wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(.,'Accept') or contains(.,'Agree')]")
            )).click()
        except Exception:
            pass

        search = wait.until(EC.element_to_be_clickable((By.NAME, "q")))
        search.clear()
        _slow_type(search, job.search_term)
        search.send_keys(Keys.ENTER)
        time.sleep(6)

        sidebar = get_sidebar()
        seen = set()
        saved = 0

        # CASE 1 — Single business loaded directly (no list)
        if not sidebar:
            time.sleep(3)
            if wait_panel():
                name = get_name()
                if name:
                    combined = get_rating_reviews()
                    category = get_category()
                    address = get_address()
                    phone = get_phone()
                    website = get_website()
                    lat, lng = get_lat_lng()
                    rating, rev_count, combined_str = _parse_rating_reviews(combined)
                    key = _unique_key(name, address, phone)
                    smart_upsert_place(job, key, {
                        'name': name, 'rating': rating, 'reviews_count': rev_count,
                        'rating_reviews': combined_str, 'category': category,
                        'address': address, 'phone': phone, 'website': website,
                        'opening_hours': get_hours(),
                        'description': get_description(),
                        'services': get_services(),
                        'secondary_categories': get_secondary_categories(),
                        'plus_code': get_plus_code(),
                        'place_url': get_place_url(),
                        'photos_count': get_photos_count(),
                        'claimed': get_claimed(),
                        'price_level': get_price_level(),
                        'latitude': lat, 'longitude': lng,
                        'source': 'google_maps',
                    })
                    saved = 1
            job.status = 'completed'
            job.total_results = saved
            job.finished_at = timezone.now()
            job.save()
            return

        # CASE 2 — List of results: scroll until end, then extract each
        prev = 0
        same = 0
        max_iterations = 80
        end_marker_seen = False

        for _ in range(max_iterations):
            # Scroll feed using JS
            try:
                driver.execute_script(
                    "arguments[0].scrollTop = arguments[0].scrollHeight;", sidebar
                )
            except Exception:
                pass
            time.sleep(2.0)

            cards = sidebar.find_elements(By.XPATH, ".//div[@role='article']")

            try:
                end_text = sidebar.find_element(
                    By.XPATH,
                    ".//*[contains(text(),\"You've reached the end\") or contains(text(),'reached the end')]"
                )
                if end_text:
                    end_marker_seen = True
            except Exception:
                pass

            if len(cards) == prev:
                same += 1
            else:
                same = 0
            prev = len(cards)
            if same >= 4 or end_marker_seen:
                break

        cards = sidebar.find_elements(By.XPATH, ".//div[@role='article']")

        for idx, card in enumerate(cards):
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'})", card
                )
                time.sleep(0.6)
                try:
                    card.click()
                except Exception:
                    try:
                        link = card.find_element(By.XPATH, ".//a[contains(@href,'/place/')]")
                        driver.execute_script("arguments[0].click();", link)
                    except Exception:
                        continue

                if not wait_panel():
                    continue
                time.sleep(1.5)

                name = get_name()
                if not name:
                    continue
                combined = get_rating_reviews()
                category = get_category()
                address = get_address()
                phone = get_phone()
                website = get_website()

                key = _unique_key(name, address, phone)
                if key in seen:
                    continue
                seen.add(key)

                rating, rev_count, combined_str = _parse_rating_reviews(combined)

                lat, lng = get_lat_lng()
                smart_upsert_place(job, key, {
                    'name': name, 'rating': rating, 'reviews_count': rev_count,
                    'rating_reviews': combined_str, 'category': category,
                    'address': address, 'phone': phone, 'website': website,
                    'opening_hours': get_hours(),
                    'description': get_description(),
                    'services': get_services(),
                    'secondary_categories': get_secondary_categories(),
                    'plus_code': get_plus_code(),
                    'place_url': get_place_url(),
                    'photos_count': get_photos_count(),
                    'claimed': get_claimed(),
                    'price_level': get_price_level(),
                    'latitude': lat, 'longitude': lng,
                    'source': 'google_maps',
                })
                saved += 1

                # Live progress: update job.total_results every 3 records
                if saved % 3 == 0:
                    ScrapeJob.objects.filter(id=job.id).update(total_results=saved)
            except Exception:
                continue

        job.status = 'completed'
        job.total_results = saved
        job.finished_at = timezone.now()
        job.save()

        # Auto-enrich newly scraped places (fetch emails + socials)
        try:
            from .enrichment import enrich_job_async
            enrich_job_async(job.id)
        except Exception:
            pass

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


def start_scrape_async(search_term, headless=True):
    """Create a job and start scraping in a background thread."""
    job = ScrapeJob.objects.create(search_term=search_term, status='pending', source='google_maps')
    t = threading.Thread(target=run_scrape, args=(job.id, headless), daemon=True)
    t.start()
    return job
