#!/usr/bin/env python3
"""
Universal Form Filler for Job Applications - Enhanced Version
Handles job listing pages, login flows, multi-step applications,
and uses DecisionHandler for post-apply choices.
"""

import re
import time
import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Union, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from datetime import datetime
from playwright.sync_api import (
    Page, Locator, Frame, ElementHandle, Browser, BrowserContext,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError
)
import random
import urllib.parse

# --- NEW IMPORT (Ensure decision_handler.py is in the same directory) ---
from decision_handler import DecisionHandler, check_and_handle_decision_points
# -----------------------------------------------------------------------

# ------------- CONFIG -----------------
JOB_FORM_URL_EXAMPLE = "https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Mount-Laurel-New-Jersey/AML-Data-Scientist-III_R_1405035"
MAX_FORM_PAGES_TO_PROCESS = 5 
DEFAULT_GOTO_TIMEOUT = 60000
DEFAULT_ACTION_TIMEOUT = 15000
DEFAULT_NAVIGATION_TIMEOUT = 45000
INTERACTION_DELAY_MS = 250

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(module)s.%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.FileHandler('universal_form_filler.log', mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ========== DATA CLASSES ==========
class FieldType(Enum):
    EMAIL = "email"
    PASSWORD = "password"
    CONFIRM_PASSWORD = "confirm_password"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FULL_NAME = "full_name"
    PHONE = "phone"
    LOCATION = "location"
    ADDRESS_LINE1 = "address_line1"
    ADDRESS_LINE2 = "address_line2"
    CITY = "city"
    STATE = "state"
    ZIP_CODE = "zip_code"
    COUNTRY = "country"
    COMPANY = "company"
    JOB_TITLE = "job_title"
    YEARS_EXPERIENCE = "years_experience"
    SALARY = "salary"
    SCHOOL = "school"
    DEGREE = "degree"
    FIELD_OF_STUDY = "field_of_study"
    GRADUATION_DATE = "graduation_date"
    LINKEDIN = "linkedin_profile_url"
    PORTFOLIO = "portfolio_url"
    WEBSITE = "personal_website_url"
    COVER_LETTER_TEXT = "cover_letter_text"
    RESUME_FILE = "resume_file"
    COVER_LETTER_FILE = "cover_letter_file"
    TEXT_INPUT = "text_input"
    SELECT = "select_dropdown"
    CHECKBOX = "checkbox"
    RADIO_BUTTON = "radio_button_group"
    TEXTAREA = "textarea"
    SUBMIT_BUTTON = "submit_button"
    NEXT_BUTTON = "next_button"
    UNKNOWN = "unknown"

@dataclass
class FormField:
    element: Locator
    field_type: FieldType
    confidence: float
    selector: str
    label_text: Optional[str] = None
    attributes: Dict[str, Optional[str]] = field(default_factory=dict)
    context_text: Optional[str] = None
    element_type_html: str = "input"
    is_in_iframe: bool = False
    iframe_selector: Optional[str] = None
    is_in_shadow_dom: bool = False
    host_selector: Optional[str] = None
    
    def __lt__(self, other: 'FormField') -> bool:
        if not isinstance(other, FormField):
            return NotImplemented
        return self.confidence < other.confidence

@dataclass
class FormContextData:
    form_title: str = ""
    form_purpose: str = ""
    is_multi_step: bool = False
    current_step: int = 1
    total_steps: int = 1

@dataclass
class FormAnalysisResult:
    url: str
    form_purpose: str = "unknown"
    is_multi_step_form: bool = False
    current_page_is_part_of_multi_step: bool = False
    current_step_on_page: int = 1
    total_steps_on_page: int = 1
    detected_fields: Dict[FieldType, FormField] = field(default_factory=dict)
    action_buttons: Dict[str, List[FormField]] = field(default_factory=lambda: {"submit": [], "next": [], "apply": []})
    errors: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

@dataclass
class FillAttemptResult:
    success: Union[bool, str] = False 
    status_message: str = ""
    fields_filled_count: int = 0
    fields_attempted_count: int = 0
    errors: List[str] = field(default_factory=list)
    skipped_fields: List[str] = field(default_factory=list)
    duration: float = 0.0
    step_number: Optional[int] = None

@dataclass
class OverallApplicationResult:
    application_url: str
    total_steps_provided_in_data: int = 0
    steps_attempted_on_site: int = 0
    steps_successfully_filled: int = 0
    total_fields_filled_across_steps: int = 0
    final_status: str = "initiated" 
    errors: List[str] = field(default_factory=list)
    step_details: List[FillAttemptResult] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

# ========== HELPER UTILITIES ==========
def safe_get_attribute(element: Locator, attribute: str, default: str = "") -> Optional[str]:
    try:
        if element.is_attached():
            return element.get_attribute(attribute) or default
        return default
    except PlaywrightError: 
        return default
    except Exception: 
        return default

def safe_get_text_content(element: Locator, default: str = "") -> str:
    try:
        if element.is_attached():
            return element.text_content() or default
        return default
    except PlaywrightError:
        return default
    except Exception:
        return default

def safe_get_tag_name(element: Locator) -> str:
    try:
        if element.is_attached():
            return element.evaluate("el => el.tagName.toLowerCase()")
        return "unknown"
    except PlaywrightError:
        return "unknown"
    except Exception:
        return "unknown"

def generate_robust_selector(element: Locator, tag_hint: Optional[str] = None) -> str:
    try:
        if not element.is_attached():
            return "detached_element"
            
        attrs_priority = ['data-testid', 'data-cy', 'id', 'name', 'data-qa', 'aria-label', 'data-automation-id']
        tag_name = tag_hint or safe_get_tag_name(element)
        
        for attr in attrs_priority:
            val = safe_get_attribute(element, attr)
            if val and not val.isnumeric() and not re.match(r'^[a-f0-9-]{20,}$', val): 
                escaped_val = re.sub(r'([!"#$%&\'()*+,./:;<=>?@\[\\\]^`{|}~])', r'\\\1', val)
                return f"{tag_name}[{attr}='{escaped_val}']"
        
        placeholder = safe_get_attribute(element, "placeholder")
        if tag_name == "input" and placeholder:
            input_type = safe_get_attribute(element, "type", "text") 
            escaped_placeholder = placeholder[:30].replace("'", "\\'") 
            return f"input[type='{input_type}'][placeholder*='{escaped_placeholder}']"
        
        class_name = safe_get_attribute(element, "class")
        if class_name:
            classes = [c for c in class_name.split() if len(c) > 3 and \
                       not c.startswith(('css-', 'sc-', 'styled__', 'style-', 'ember', 'm-', 'p-', 'w-', 'h-')) and \
                       c not in ['input', 'form-control', 'field', 'button', 'label', 'active', 'focus']]
            if classes:
                return f"{tag_name}.{classes[0]}" 
                
        return tag_name if tag_name != "unknown" else "unknown_selector"
    except Exception as e:
        logger.debug(f"Error generating robust selector: {e}")
        return "unknown_selector_error"

def get_domain(url: str) -> str: 
    try:
        parsed_url = urllib.parse.urlparse(url)
        return parsed_url.netloc
    except Exception:
        logger.warning(f"Could not parse domain from URL: {url}")
        return "unknown_domain"

# ========== PAGE NAVIGATION HELPERS ==========
def handle_job_listing_page(page: Page) -> bool:
    try:
        logger.info("Checking for Apply button on job listing page...")
        apply_selectors = [
            "button:has-text('Apply')", "a:has-text('Apply')", 
            "button:has-text('Apply Now')", "a:has-text('Apply Now')",
            "button:has-text('Apply for this job')", "a:has-text('Apply for this job')",
            "[data-automation-id*='apply']", "[aria-label*='apply i']", 
            "[data-uxi-element-id*='apply']",
            "[data-automation-id='jobdetails-applybutton']", 
            "[data-testid='JobDetailsApplyButton']", 
            "button[name='Apply']",
        ]
        page.wait_for_timeout(INTERACTION_DELAY_MS * 2) 
        
        for selector in apply_selectors:
            try:
                elements = page.locator(selector).all()
                for element in elements:
                    if element.is_visible(timeout=1000) and element.is_enabled(timeout=1000):
                        text_content = (safe_get_text_content(element) or "").lower()
                        aria_label_content = (safe_get_attribute(element, "aria-label") or "").lower()
                        negative_keywords = ["save", "share", "linkedin", "indeed", "later"]
                        if any(keyword in text_content for keyword in negative_keywords) or \
                           any(keyword in aria_label_content for keyword in negative_keywords):
                            if "apply" in text_content or "apply" in aria_label_content : 
                                logger.debug(f"Skipping apply variant: {text_content or aria_label_content} due to keywords.")
                            continue

                        logger.info(f"Found Apply button with selector: {selector} (Text: '{text_content}')")
                        element.scroll_into_view_if_needed(timeout=2000)
                        page.wait_for_timeout(INTERACTION_DELAY_MS // 2) 
                        element.click(timeout=DEFAULT_ACTION_TIMEOUT // 2)
                        logger.info("Clicked Apply button, waiting for potential navigation...")
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=10000)
                        except PlaywrightTimeoutError:
                            logger.warning("Timeout waiting for domcontentloaded after Apply click. Page might be SPA or slow.")
                        try:
                             page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeoutError:
                            logger.warning("Timeout waiting for networkidle after Apply click.")
                        page.wait_for_timeout(INTERACTION_DELAY_MS * 4) 
                        return True
            except PlaywrightTimeoutError: 
                logger.debug(f"Apply button with selector {selector} not interactable in time.")
            except Exception as e:
                logger.debug(f"Error with Apply selector {selector}: {e}")
        
        logger.warning("No primary Apply button found on job listing page after trying common selectors.")
        try:
            page.screenshot(path="debug_no_apply_button_found.png", full_page=True)
            logger.info("Screenshot saved: debug_no_apply_button_found.png")
        except Exception as ss_err:
            logger.error(f"Failed to save no_apply_button screenshot: {ss_err}")
        return False
    except Exception as e:
        logger.error(f"Error handling job listing page: {e}", exc_info=True)
        return False

def handle_login_page(page: Page, email: str, password: str) -> bool:
    try:
        current_url_lower = page.url.lower()
        logger.info(f"Checking if on login page... Current URL: {current_url_lower[:100]}")
        login_indicators = ["signin", "login", "auth", "sso", "workday.com/auth", "accountlogin"]
        has_email_field = page.locator("input[type='email'], input[name*='email'], input[id*='email'], [data-automation-id='email']").count() > 0
        has_password_field = page.locator("input[type='password'], input[name*='password'], input[id*='password'], [data-automation-id='password']").count() > 0

        if not (any(indicator in current_url_lower for indicator in login_indicators) or (has_email_field and has_password_field)):
            logger.info("Not definitively on a login page based on URL or initial field scan.")
            return False
            
        logger.info("Detected potential login page, attempting to fill credentials...")
        page.wait_for_timeout(INTERACTION_DELAY_MS * 3) 
        email_selectors = [
            "[data-automation-id='email'], [data-automation-id='username']", 
            "input[type='email']", "input[name*='email'], input[id*='email']",
            "input[name*='username'], input[id*='username']",
            "input[placeholder*='email' i]", "input[placeholder*='username' i]",
            "input[aria-label*='email' i]", "input[aria-label*='username' i]",
        ]
        email_filled = False
        for selector in email_selectors:
            try:
                elements = page.locator(selector).all()
                for email_field in elements:
                    if email_field.is_visible(timeout=500) and email_field.is_enabled(timeout=500):
                        logger.info(f"Found email field with selector part: {selector}")
                        email_field.scroll_into_view_if_needed(timeout=1000)
                        email_field.click(delay=random.randint(30,80))
                        email_field.fill("") 
                        email_field.type(email, delay=random.randint(40, 120))
                        email_filled = True
                        break
                if email_filled: break
            except Exception: continue
            
        if not email_filled:
            logger.warning("Could not find or fill email/username field on login page.")
            return False
            
        next_button_selectors = [
            "button:has-text('Next' i)", "button:has-text('Continue' i)",
            "input[type='submit'][value*='Next' i]", "input[type='submit'][value*='Continue' i]"
        ]
        next_clicked = False
        for selector in next_button_selectors:
            try:
                elements = page.locator(selector).all()
                for next_button in elements:
                     if next_button.is_visible(timeout=500) and next_button.is_enabled(timeout=500):
                        logger.info("Found Next/Continue button after email, clicking...")
                        next_button.click(delay=random.randint(50,100))
                        page.wait_for_timeout(INTERACTION_DELAY_MS * 8) 
                        next_clicked = True
                        break
                if next_clicked: break
            except Exception: continue

        password_selectors = [
            "[data-automation-id='password']", 
            "input[type='password']",
            "input[name*='password' i]", "input[id*='password' i]",
            "input[placeholder*='password' i]", "input[aria-label*='password' i]",
        ]
        password_filled = False
        for selector in password_selectors:
            try:
                elements = page.locator(selector).all()
                for password_field in elements:
                    if password_field.is_visible(timeout=500) and password_field.is_enabled(timeout=500):
                        logger.info(f"Found password field with selector part: {selector}")
                        password_field.scroll_into_view_if_needed(timeout=1000)
                        password_field.click(delay=random.randint(30,80))
                        password_field.fill("") 
                        password_field.type(password, delay=random.randint(40, 120))
                        password_filled = True
                        break
                if password_filled: break
            except Exception: continue
            
        if not password_filled:
            logger.warning("Could not find or fill password field.")
            return False if not next_clicked else True 
            
        signin_selectors = [
            "[data-automation-id='signInSubmitButton']", 
            "button[type='submit']",
            "button:has-text('Sign In' i)", "button:has-text('Log In' i)",
            "button:has-text('Submit' i)", "button:has-text('Continue' i)", 
            "input[type='submit'][value*='Sign In' i]", "input[type='submit'][value*='Log In' i]",
        ]
        signin_button_clicked = False
        for selector in signin_selectors:
            try:
                elements = page.locator(selector).all()
                for signin_button in elements:
                    if signin_button.is_visible(timeout=1000) and signin_button.is_enabled(timeout=1000):
                        logger.info(f"Found sign-in button with selector part: {selector}")
                        signin_button.scroll_into_view_if_needed(timeout=1000)
                        signin_button.click(delay=random.randint(50,150))
                        logger.info("Clicked sign-in button, waiting for navigation...")
                        signin_button_clicked = True
                        break
                if signin_button_clicked: break
            except Exception: continue

        if not signin_button_clicked:
            logger.warning("Could not find or click sign-in button.")
            return False

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            logger.warning("Timeout waiting for networkidle after sign-in click.")
        page.wait_for_timeout(INTERACTION_DELAY_MS * 10) 
        
        final_url_lower = page.url.lower()
        if not any(indicator in final_url_lower for indicator in login_indicators) or final_url_lower != current_url_lower:
            error_messages = ["incorrect", "invalid", "failed", "try again", "doesn't match"]
            page_text = page.content().lower() # Potential performance hit
            if any(err_msg in page_text for err_msg in error_messages):
                login_error_locator = page.locator("[class*='error' i], [class*='alert' i], [role='alert']")
                if login_error_locator.count() > 0:
                    for i in range(login_error_locator.count()):
                        err_text = (login_error_locator.nth(i).text_content() or "").lower()
                        if any(msg_part in err_text for msg_part in ["password", "email", "username", "credential"]):
                            logger.warning(f"Login failed, error message found: {err_text}")
                            return False
            logger.info("Login appears successful - navigated away or page content changed from login indicators.")
            return True
        else:
            logger.warning("Still on a login-like page after clicking sign-in or URL unchanged.")
            return False
    except Exception as e:
        logger.error(f"Error handling login page: {e}", exc_info=True)
        return False

def check_for_create_account_option(page: Page) -> bool:
    try:
        create_account_selectors = [
            "a:has-text('Create Account' i)", "button:has-text('Create Account' i)",
            "a:has-text('Sign Up' i)", "button:has-text('Sign Up' i)",
            "a:has-text('Register' i)", "button:has-text('Register' i)",
            "[data-automation-id='createAccount']"
        ]
        for selector in create_account_selectors:
            try:
                elements = page.locator(selector).all()
                for element in elements:
                    if element.is_visible(timeout=500):
                        logger.info(f"Found 'Create Account' option with text/selector: '{safe_get_text_content(element).strip()}' / {selector}")
                        return True
            except Exception: continue
        return False
    except Exception as e:
        logger.debug(f"Error checking for create account: {e}")
        return False

# ========== HELPER CLASSES ==========
class AntiDetectionManager:
    def __init__(self, page: Page):
        self.page = page
        
    def random_delay(self, min_ms: int = 200, max_ms: int = 600):
        delay_s = random.randint(min_ms, max_ms) / 1000.0
        logger.debug(f"Anti-detection: Delay: {delay_s:.2f}s")
        self.page.wait_for_timeout(delay_s * 1000) 
        
    def human_type(self, element: Locator, text: str, min_char_delay_ms: int = 30, max_char_delay_ms: int = 110):
        logger.debug(f"Anti-detection: Human typing text starting with: '{text[:20]}...'")
        try:
            element.scroll_into_view_if_needed(timeout=1000)
            element.click(delay=random.randint(40, 100), timeout=DEFAULT_ACTION_TIMEOUT//2)
            self.random_delay(100, 200)
            element.fill("") 
            
            for idx, char in enumerate(text):
                element.type(char, delay=random.randint(min_char_delay_ms, max_char_delay_ms))
                if idx > 0 and idx % random.randint(8, 15) == 0: 
                    self.page.wait_for_timeout(random.uniform(0.05, 0.15) * 1000)
            
            for event_name in ["input", "change", "blur"]:
                try: element.dispatch_event(event_name)
                except PlaywrightError: pass 
        except PlaywrightError as e:
            logger.warning(f"Anti-detection: PlaywrightError during human_type: {e}. Falling back to fill.")
            try: 
                if element.is_attached() and element.is_enabled():
                    element.fill(text)
            except Exception as fill_e:
                 logger.error(f"Anti-detection: Fallback fill also failed: {fill_e}")
        except Exception as e_gen:
            logger.error(f"Anti-detection: Generic error during human_type: {e_gen}")

class DOMStabilityManager:
    def __init__(self, page: Page):
        self.page = page
        self._observer_script_injected = False
        
    def _inject_stability_observer_script(self):
        if self._observer_script_injected:
            try:
                self.page.evaluate("if(window.stabilityDetector) { window.stabilityDetector.lastCriticalMutation = Date.now(); window.stabilityDetector.startObserving(window.stabilityDetector.stabilityWindowMs); }")
            except PlaywrightError: 
                self._observer_script_injected = False 
            except Exception as e:
                 logger.warning(f"Error re-initializing stability observer: {e}")
                 self._observer_script_injected = False
            if self._observer_script_injected: return 

        try:
            self.page.evaluate("""
                window.stabilityDetector = {
                    mutationCount: 0, criticalMutations: 0, lastCriticalMutation: Date.now(),
                    stabilityWindowMs: 1500, observer: null, timerId: null,
                    isCriticalMutation: function(mutation) {
                        if (mutation.type === 'childList' && (mutation.addedNodes.length > 0 || mutation.removedNodes.length > 0)) return true;
                        if (mutation.type === 'attributes') {
                            const target = mutation.target;
                            if (target && typeof target.matches === 'function' &&
                                target.matches('input,select,textarea,button,[role="button"],[role="textbox"],[role="combobox"],[role="listbox"],[role="option"],[role="checkbox"],[role="radio"]')) {
                                if (['disabled', 'hidden', 'style', 'class', 'value', 'checked', 'selected', 'readonly', 'aria-disabled', 'aria-hidden'].includes(mutation.attributeName)) return true;
                            }
                            if (mutation.attributeName === 'style' && (mutation.oldValue || target.style.display === 'none' || target.style.visibility === 'hidden')) return true;
                        }
                        return false;
                    },
                    processMutations: function(mutationsList) {
                        this.mutationCount += mutationsList.length;
                        let criticalFound = false;
                        for(let mutation of mutationsList) {
                            if (this.isCriticalMutation(mutation)) {
                                this.lastCriticalMutation = Date.now(); this.criticalMutations++; criticalFound = true; break; 
                            }
                        }
                    },
                    startObserving: function(stabilityWindow = 1500) {
                        this.stabilityWindowMs = stabilityWindow; this.lastCriticalMutation = Date.now(); 
                        if (this.observer) this.observer.disconnect();
                        this.observer = new MutationObserver(this.processMutations.bind(this));
                        this.observer.observe(document.documentElement, {
                            childList: true, subtree: true, attributes: true, 
                            attributeOldValue: true, characterData: false 
                        });
                    },
                    isStable: function() {
                        const quietPeriod = Date.now() - this.lastCriticalMutation;
                        return quietPeriod >= this.stabilityWindowMs;
                    },
                    getMetrics: function() { return {}; }, stopObserving: function() { if(this.observer) {this.observer.disconnect(); this.observer=null;} }
                };
                if(window.stabilityDetector && typeof window.stabilityDetector.startObserving === 'function') {
                    window.stabilityDetector.startObserving();
                }
            """)
            self._observer_script_injected = True
            logger.debug("DOM Stability Intelligent Observer injected and started.")
        except PlaywrightError as e: 
            logger.error(f"PlaywrightError injecting DOM stability script (page might be closed/navigated): {e}")
            self._observer_script_injected = False
        except Exception as e_gen:
            logger.error(f"Generic error injecting DOM stability script: {e_gen}")
            self._observer_script_injected = False
            
    def wait_for_intelligent_stability(self, timeout: int = 10000, stability_check_window_ms: int = 1000) -> bool:
        if self.page.is_closed():
            logger.warning("Page closed, cannot wait for DOM stability.")
            return False
            
        logger.info(f"Waiting for intelligent DOM stability (timeout: {timeout/1000}s, window: {stability_check_window_ms/1000}s)...")
        
        if not self._observer_script_injected:
             self._inject_stability_observer_script()
        
        if not self._observer_script_injected:
            logger.warning("Observer script not injected/failed. Using networkidle + fixed delay for stability.")
            try:
                self.page.wait_for_load_state("networkidle", timeout=max(3000, timeout // 2))
                self.page.wait_for_timeout(stability_check_window_ms) 
                return True
            except PlaywrightTimeoutError:
                logger.warning("Networkidle timeout during fallback stability.")
                return False
            except Exception as e:
                logger.error(f"Error in fallback stability wait: {e}")
                return False
        
        try:
            self.page.evaluate(
                f"if(window.stabilityDetector) {{ window.stabilityDetector.stabilityWindowMs = {stability_check_window_ms}; window.stabilityDetector.startObserving({stability_check_window_ms}); }}"
            )
        except Exception as e: 
            logger.warning(f"Could not re-configure/restart DOM observer (page might have changed): {e}. Relying on previous start or fallback.")

        start_time = time.time()
        loop_count = 0
        initial_network_idle_achieved = False

        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout//3)
            initial_network_idle_achieved = True
            logger.debug("Initial networkidle achieved before stability loop.")
        except PlaywrightTimeoutError:
            logger.debug("Initial networkidle not achieved, proceeding with stability checks.")
        except PlaywrightError: 
             if self.page.is_closed(): return False

        while (time.time() - start_time) * 1000 < timeout:
            if self.page.is_closed():
                logger.warning("Page closed during stability check.")
                return False
            loop_count += 1
            try:
                is_stable = self.page.evaluate("window.stabilityDetector && typeof window.stabilityDetector.isStable === 'function' ? window.stabilityDetector.isStable() : true")
                if is_stable:
                    if initial_network_idle_achieved or loop_count > 3 : 
                        try:
                            self.page.wait_for_load_state("networkidle", timeout=stability_check_window_ms) 
                            logger.info(f"DOM achieved intelligent stability after ~{int((time.time() - start_time)*1000)}ms in {loop_count} checks (Network calm).")
                            return True
                        except PlaywrightTimeoutError:
                            logger.debug(f"DOM stable by mutation observer, but network still active. Loop: {loop_count}. Continuing wait.")
                            self.page.evaluate("if(window.stabilityDetector) { window.stabilityDetector.lastCriticalMutation = Date.now(); }")
                    else: 
                         logger.info(f"DOM achieved intelligent stability (observer) after ~{int((time.time() - start_time)*1000)}ms in {loop_count} checks.")
                         return True
            except PlaywrightError as e: 
                logger.warning(f"PlaywrightError checking DOM stability (page might have navigated/closed): {e}")
                return False 
            except Exception as e_gen:
                logger.error(f"Generic error in intelligent stability check loop: {e_gen}")
                return False 
            self.page.wait_for_timeout(250) 
            
        logger.warning(f"DOM intelligent stability timeout ({timeout/1000}s) reached after {loop_count} checks.")
        return False

# ========== FIELD DETECTOR ==========
class FieldDetector:
    def __init__(self, page: Page, config: Dict[str, Any], filler_instance: 'UniversalFormFiller'):
        self.page = page
        self.config = config
        self.filler = filler_instance 
        self.field_patterns = filler_instance._get_default_field_patterns() 
        self.negative_patterns = filler_instance._get_default_negative_patterns()
        self.attribute_weights = filler_instance._get_default_attribute_weights()
        logger.info(f"FieldDetector initialized with patterns for {len(self.field_patterns)} types.")
        
    def detect_all_fields_on_page(self, current_page_or_frame: Union[Page, Frame], form_selector_str: str) -> FormAnalysisResult:
        analysis = FormAnalysisResult(url=current_page_or_frame.url)
        try:
            base_locator = current_page_or_frame.locator(form_selector_str) if form_selector_str != "body" else current_page_or_frame
            element_css_selectors = [
                "input:not([type='hidden']):not([type='image'])", 
                "select", "textarea", "button",
                "[role='textbox']", "[role='combobox']", "[role='listbox']",
                "[role='checkbox']", "[role='radio']", "[role='switch']", 
                "[contenteditable='true']" 
            ]
            all_elements_locators: List[Locator] = []
            for css_selector in element_css_selectors:
                try:
                    elements = base_locator.locator(css_selector).all()
                    all_elements_locators.extend(elements)
                except PlaywrightError as e:
                    logger.debug(f"Error locating elements with '{css_selector}' in '{form_selector_str}': {e}")
                except Exception: pass

            logger.info(f"Found {len(all_elements_locators)} potential form elements within '{form_selector_str}'.")
            field_candidates: List[FormField] = []
            
            for element_loc in all_elements_locators:
                try:
                    if not element_loc.is_attached() or not element_loc.is_visible(timeout=100): 
                        continue
                    element_data = self._get_element_data(element_loc) 
                    if not element_data: continue
                    
                    html_tag = element_data.get('tag', 'unknown')
                    html_type = element_data.get('type', '')
                    is_button_like = html_tag in ['button', 'a'] or \
                                     html_type in ['submit', 'button', 'reset', 'image'] or \
                                     "button" in element_data.get('role', '')

                    if is_button_like:
                        button_classification = self._classify_button(element_data)
                        if button_classification and button_classification in analysis.action_buttons:
                            field = FormField(
                                element=element_loc, 
                                field_type=FieldType.SUBMIT_BUTTON if button_classification == 'submit' else \
                                           (FieldType.NEXT_BUTTON if button_classification == 'next' else FieldType.UNKNOWN), 
                                confidence=0.85, 
                                selector=element_data.get('selector', 'unknown_button_selector'),
                                label_text=element_data.get('text') or element_data.get('aria-label'), 
                                attributes=element_data
                            )
                            analysis.action_buttons[button_classification].append(field)
                            continue 
                            
                    possible_types_for_element: List[Tuple[float, FieldType]] = []
                    for ft_enum, patterns_for_type in self.field_patterns.items():
                        confidence = self._calculate_confidence(element_data, patterns_for_type, ft_enum)
                        if confidence > 0.15: 
                            possible_types_for_element.append((confidence, ft_enum))
                    
                    if possible_types_for_element:
                        possible_types_for_element.sort(key=lambda x: x[0], reverse=True)
                        best_confidence, best_field_type = possible_types_for_element[0]
                        if best_confidence >= self.config.get('confidence_threshold', 0.45):
                             field = FormField(
                                element=element_loc, 
                                field_type=best_field_type,
                                confidence=best_confidence,
                                selector=element_data.get('selector', 'unknown_field_selector'),
                                label_text=element_data.get('label'),
                                attributes=element_data,
                                context_text=element_data.get('context'),
                                element_type_html=element_data.get('tag', 'unknown')
                            )
                             field_candidates.append(field)
                except PlaywrightTimeoutError: 
                    logger.debug(f"Element {generate_robust_selector(element_loc)} no longer visible during detection loop.")
                except PlaywrightError as pe: 
                    logger.debug(f"PlaywrightError processing element {generate_robust_selector(element_loc)}: {pe}")
                except Exception as e_analyze:
                    logger.debug(f"Error analyzing specific element: {e_analyze}", exc_info=False)
            
            analysis.detected_fields = self._resolve_field_conflicts(field_candidates)
            analysis.form_purpose = self._detect_form_purpose(analysis.detected_fields) 
            
            step_info = self._detect_multi_step_indicators(current_page_or_frame)
            analysis.is_multi_step_form = step_info.get('is_multi_step', False)
            analysis.current_step_on_page = step_info.get('current_step', 1)
            analysis.total_steps_on_page = step_info.get('total_steps', 1)
            
            logger.info(f"Field detection complete: {len(analysis.detected_fields)} fields resolved. "
                        f"Action buttons: Submit({len(analysis.action_buttons.get('submit',[]))}), "
                        f"Next({len(analysis.action_buttons.get('next',[]))}), Apply({len(analysis.action_buttons.get('apply',[]))}).")
        except PlaywrightError as e_page: 
            logger.error(f"PlaywrightError in detect_all_fields_on_page for URL {analysis.url[:70]}: {e_page}")
            analysis.errors.append(f"PlaywrightError during field detection: {e_page}")
        except Exception as e:
            logger.error(f"Critical error in detect_all_fields_on_page: {e}", exc_info=True)
            analysis.errors.append(str(e))
        return analysis

    def _get_element_data(self, element_loc: Locator) -> Optional[Dict[str, Any]]:
        try:
            tag_name = safe_get_tag_name(element_loc)
            attrs = {
                'tag': tag_name,
                'type': safe_get_attribute(element_loc, 'type', default='text' if tag_name == 'input' else ''),
                'name': safe_get_attribute(element_loc, 'name'),
                'id': safe_get_attribute(element_loc, 'id'),
                'class': safe_get_attribute(element_loc, 'class'),
                'placeholder': safe_get_attribute(element_loc, 'placeholder'),
                'aria-label': safe_get_attribute(element_loc, 'aria-label'),
                'aria-labelledby': safe_get_attribute(element_loc, 'aria-labelledby'),
                'aria-describedby': safe_get_attribute(element_loc, 'aria-describedby'),
                'role': safe_get_attribute(element_loc, 'role'),
                'autocomplete': safe_get_attribute(element_loc, 'autocomplete'),
                'required': element_loc.evaluate_handle("el => el.required").json_value() if tag_name in ['input', 'select', 'textarea'] else False,
                'value': safe_get_attribute(element_loc, 'value'), 
                'text': safe_get_text_content(element_loc).strip(), 
                'data-automation-id': safe_get_attribute(element_loc, 'data-automation-id'),
                'selector': generate_robust_selector(element_loc, tag_hint=tag_name)
            }
            attrs['label'] = self.filler._get_field_label(element_loc, attrs.get('id'), attrs.get('aria-labelledby'))
            return attrs
        except PlaywrightError as e: 
            logger.debug(f"PlaywrightError getting element data for {generate_robust_selector(element_loc)}: {e}")
            return None
        except Exception as e_gen:
            logger.debug(f"Generic error getting element data: {e_gen}", exc_info=False)
            return None

    def _calculate_confidence(self, element_data: Dict[str, Any], patterns_for_type: Dict[str, List[str]], field_type: FieldType) -> float:
        score = 0.0
        weights = self.attribute_weights 
        text_attributes_to_check = ['label', 'name', 'id', 'placeholder', 'aria-label', 'text', 'data-automation-id', 'autocomplete']
        
        for pattern_group_key, regex_list in patterns_for_type.items(): 
            element_attr_to_match_val = None
            if pattern_group_key == 'names': element_attr_to_match_val = element_data.get('name')
            elif pattern_group_key == 'labels': element_attr_to_match_val = element_data.get('label')
            elif pattern_group_key == 'placeholders': element_attr_to_match_val = element_data.get('placeholder')
            elif pattern_group_key == 'types': element_attr_to_match_val = element_data.get('type')
            elif pattern_group_key == 'autocompletes': element_attr_to_match_val = element_data.get('autocomplete')
            elif pattern_group_key == 'data-automation-ids': element_attr_to_match_val = element_data.get('data-automation-id')
            elif pattern_group_key == 'texts': element_attr_to_match_val = element_data.get('text')
            
            if not element_attr_to_match_val: continue

            attr_value_lower = str(element_attr_to_match_val).lower()
            weight_for_this_attr_patterns = weights.get(pattern_group_key[:-1] if pattern_group_key.endswith('s') else pattern_group_key, 1.0)

            for pattern_regex in regex_list:
                try:
                    if re.search(pattern_regex, attr_value_lower, re.IGNORECASE):
                        score += weight_for_this_attr_patterns
                        break 
                except re.error:
                    logger.warning(f"Invalid regex '{pattern_regex}' for {field_type.value}/{pattern_group_key}")
        
        html_type = element_data.get('type', '').lower()
        if html_type == field_type.value and field_type in [FieldType.EMAIL, FieldType.PASSWORD]: 
            score += weights.get('type', 3.0) * 1.5 
        elif html_type == 'tel' and field_type == FieldType.PHONE:
            score += weights.get('type', 3.0)
        elif html_type == 'file' and field_type in [FieldType.RESUME_FILE, FieldType.COVER_LETTER_FILE]:
            score += weights.get('type', 3.0)
        
        negative_regex_list = self.negative_patterns.get(field_type, [])
        if negative_regex_list:
            penalty_applied = False
            for neg_regex in negative_regex_list:
                for attr_to_check_neg in text_attributes_to_check: 
                    val = str(element_data.get(attr_to_check_neg, '')).lower()
                    if val:
                        try:
                            if re.search(neg_regex, val, re.IGNORECASE):
                                logger.debug(f"Negative pattern '{neg_regex}' matched for {field_type.value} on attr '{attr_to_check_neg}': '{val}'. Reducing score.")
                                score *= 0.3 
                                penalty_applied = True; break 
                        except re.error:
                             logger.warning(f"Invalid negative regex '{neg_regex}' for {field_type.value}")
                if penalty_applied: break

        max_possible_score = sum(w for key, w in weights.items() if key in patterns_for_type or key == 'type') + 2.0 
        if max_possible_score == 0: max_possible_score = 10.0 
        normalized_score = min(score / max_possible_score, 1.0) if score > 0 else 0.0
        return round(normalized_score, 3)

    def _resolve_field_conflicts(self, candidates: List[FormField]) -> Dict[FieldType, FormField]:
        resolved: Dict[FieldType, FormField] = {}
        elements_candidates: Dict[str, List[FormField]] = {} 
        for ff_candidate in candidates:
            element_key = ff_candidate.selector 
            if element_key not in elements_candidates: elements_candidates[element_key] = []
            elements_candidates[element_key].append(ff_candidate)

        best_candidates_per_element: List[FormField] = []
        for element_key, ff_list in elements_candidates.items():
            if not ff_list: continue
            ff_list.sort(key=lambda ff: ff.confidence, reverse=True)
            best_candidates_per_element.append(ff_list[0]) 
        
        final_by_type: Dict[FieldType, List[FormField]] = {}
        for ff_candidate in best_candidates_per_element:
            if ff_candidate.field_type not in final_by_type: final_by_type[ff_candidate.field_type] = []
            final_by_type[ff_candidate.field_type].append(ff_candidate)
            
        for ft, ff_options in final_by_type.items():
            if not ff_options: continue
            ff_options.sort(key=lambda ff: ff.confidence, reverse=True) 
            threshold = self.config.get('confidence_threshold', 0.45)
            best_ff_for_type = ff_options[0]

            if best_ff_for_type.confidence >= threshold:
                if ft not in resolved or best_ff_for_type.confidence > resolved[ft].confidence:
                    resolved[ft] = best_ff_for_type
                    logger.debug(f"Resolved field: Type={ft.value}, Confidence={best_ff_for_type.confidence:.2f}, Selector='{best_ff_for_type.selector}'")
            else:
                logger.debug(f"Field type {ft.value} best candidate (selector='{best_ff_for_type.selector}') confidence {best_ff_for_type.confidence:.2f} < threshold {threshold}. Ignoring.")
        return resolved
        
    def _classify_button(self, element_data: Dict[str, Any]) -> Optional[str]:
        text_sources = [
            element_data.get('text', ''), element_data.get('value', ''), 
            element_data.get('aria-label', ''), element_data.get('name', ''), 
            element_data.get('id', ''), element_data.get('data-automation-id', '')
        ]
        full_text_corpus = " ".join(filter(None, text_sources)).lower()
        if not full_text_corpus.strip(): return None 

        apply_patterns = [r'\bapply now\b', r'\bapply for this job\b', r'\bsubmit application\b', r'\bapply\b']
        for pattern in apply_patterns:
            if re.search(pattern, full_text_corpus): return 'apply'
                
        next_patterns = [r'\bnext\b', r'\bcontinue\b', r'\bproceed\b', r'\bstep \d+\b', r'\bforward\b']
        for pattern in next_patterns:
            if re.search(pattern, full_text_corpus):
                if r'\bsave\b' in full_text_corpus and r'\bcontinue\b' in full_text_corpus: return 'submit' 
                return 'next'

        submit_patterns = [r'\bsubmit\b', r'\bsend\b', r'\bfinish\b', r'\bcomplete\b', r'\bdone\b', r'\bsave & exit\b', r'\bsave and exit\b', r'\bsave\b']
        for pattern in submit_patterns:
            if re.search(pattern, full_text_corpus): return 'submit'
        
        if element_data.get('type', '').lower() == 'submit': return 'submit'
        return None 

    def _detect_form_purpose(self, detected_fields: Dict[FieldType, FormField]) -> str:
        field_types_present = set(detected_fields.keys())
        job_app_keywords = {
            FieldType.RESUME_FILE, FieldType.COVER_LETTER_FILE, FieldType.LINKEDIN,
            FieldType.COMPANY, FieldType.JOB_TITLE, FieldType.YEARS_EXPERIENCE,
            FieldType.SCHOOL, FieldType.DEGREE
        }
        if FieldType.RESUME_FILE in field_types_present or \
           (FieldType.JOB_TITLE in field_types_present and FieldType.COMPANY in field_types_present) or \
           len(field_types_present.intersection(job_app_keywords)) >= 2: 
            return "job_application"
            
        if FieldType.EMAIL in field_types_present and FieldType.PASSWORD in field_types_present:
            other_fields = field_types_present - {FieldType.EMAIL, FieldType.PASSWORD, FieldType.SUBMIT_BUTTON, FieldType.NEXT_BUTTON}
            if not other_fields or len(other_fields) <= 1: 
                return "login"

        if FieldType.EMAIL in field_types_present and FieldType.PASSWORD in field_types_present and \
           (FieldType.FIRST_NAME in field_types_present or FieldType.CONFIRM_PASSWORD in field_types_present or FieldType.FULL_NAME in field_types_present):
            return "registration"
            
        if (FieldType.EMAIL in field_types_present and \
            (FieldType.FULL_NAME in field_types_present or (FieldType.FIRST_NAME in field_types_present and FieldType.LAST_NAME in field_types_present)) and \
            (FieldType.TEXTAREA in field_types_present or FieldType.TEXT_INPUT in field_types_present)): 
            for ft, ff in detected_fields.items():
                if ft in [FieldType.TEXTAREA, FieldType.TEXT_INPUT]:
                    text_content = (ff.label_text or "") + " " + (ff.attributes.get('placeholder', ''))
                    if any(kw in text_content.lower() for kw in ["message", "comment", "query", "question", "feedback"]):
                        return "contact"
            if len(field_types_present) <= 5 : return "contact" 
        return "general_form"

    def _detect_multi_step_indicators(self, page_or_frame: Union[Page, Frame]) -> Dict[str, Any]:
        results = {'is_multi_step': False, 'current_step': 1, 'total_steps': 1}
        try:
            step_indicator_selectors = [
                "[class*='step']:visible", "[class*='progress']:visible", "[class*='wizard']:visible", 
                "[role='tablist']:has([role='tab'][aria-selected='true'])", 
                ".breadcrumb li.active", ".pagination .active", 
                "[data-step]:visible", "[aria-current='step']", 
                "[data-automation-id*='progressBar'] li[data-automation-id*='selected']",
                "[data-automation-id*='stepIndicator']"
            ]
            text_pattern_regex = re.compile(r'(?:step\s*)?(\d+)\s*(?:of|\/|from)\s*(\d+)', re.IGNORECASE)

            for selector in step_indicator_selectors:
                try:
                    elements = page_or_frame.locator(selector).all()
                    if not elements: continue
                    results['is_multi_step'] = True
                    
                    for el in elements:
                        if not el.is_visible(timeout=100): continue 
                        el_text = (safe_get_text_content(el) + " " + safe_get_attribute(el, "aria-label","")).strip()
                        match = text_pattern_regex.search(el_text)
                        if match:
                            current_s, total_s = int(match.group(1)), int(match.group(2))
                            if total_s > 1: 
                                results['current_step'] = current_s; results['total_steps'] = total_s
                                logger.debug(f"Multi-step detected via text: Step {current_s} of {total_s} from '{el_text[:50]}...' (Selector: {selector})")
                                return results 

                        if "progress" in selector or "wizard" in selector or "tablist" in selector or "breadcrumb" in selector :
                            total_items_loc = el.locator("li, [role='tab'], div[class*='step-item']") 
                            total_items_count = total_items_loc.count()
                            if total_items_count > 1:
                                results['total_steps'] = total_items_count
                                active_item_loc = el.locator("li.active, li[class*='current'], li[class*='active'], [role='tab'][aria-selected='true'], div[class*='current'], div[class*='active']")
                                # if active_item_loc.count() == 1: pass # Current step might remain 1
                                logger.debug(f"Multi-step inferred: {total_items_count} items in {selector}.")
                                return results 
                    if results['is_multi_step']: 
                        logger.debug(f"Multi-step inferred from selector '{selector}' presence, but no specific step numbers extracted.")
                        return results 
                except PlaywrightTimeoutError: pass 
                except Exception: pass 
        except Exception as e: 
            logger.debug(f"Error detecting multi-step indicators: {e}")
        return results 
        
    def _is_element_visible(self, element: Locator) -> bool: 
        try:
            return element.is_visible() and element.is_enabled() 
        except PlaywrightError: 
            return False
        except Exception:
            return False

# ========== OTHER HELPER CLASSES (ShadowDOMHandler, CustomComponentHandler, etc. kept as is from original) ==========
class ShadowDOMHandler:
    def __init__(self, page: Page): self.page = page
    def is_shadow_element(self, element: Locator) -> bool:
        try: return element.evaluate("el => { let c = el; while(c) { if(c.getRootNode() instanceof ShadowRoot) return true; c = c.parentNode; } return false; }")
        except: return False
    def fill_shadow_element(self, element: Locator, value: str) -> bool: 
        try: element.fill(value); return True
        except Exception as e_fill:
            logger.debug(f"Direct fill failed for shadow element: {e_fill}. Trying JS eval fill.")
            try:
                element.evaluate("el => el.value = arguments[0]", arg=value)
                element.dispatch_event('input'); element.dispatch_event('change'); element.dispatch_event('blur')
                return True
            except Exception as e_eval:
                logger.warning(f"JS eval fill also failed for shadow element: {e_eval}. Trying coordinate click and type.")
                try:
                    bbox = element.bounding_box()
                    if bbox:
                        self.page.mouse.click(bbox['x'] + bbox['width'] / 2, bbox['y'] + bbox['height'] / 2)
                        self.page.keyboard.press("Control+A"); self.page.keyboard.type(value, delay=50)
                        return True
                except Exception as e_mouse: logger.error(f"Coordinate-based fill failed for shadow element: {e_mouse}")
        return False

class CustomComponentHandler: 
    def __init__(self, page: Page): self.page = page
    def is_custom_component(self, element: Locator, element_data: Dict[str, Any]) -> bool: # Added element_data
        try:
            tag_name = element_data.get('tag', '')
            if tag_name not in ['input', 'select', 'textarea', 'button']:
                role = element_data.get('role', '')
                if role in ['combobox', 'listbox', 'textbox', 'searchbox', 'slider', 'datepicker']: return True 
            class_name = element_data.get('class', '').lower() # Use element_data
            custom_indicators = ['select2', 'chosen', 'multiselect', 'datepicker', 'calendar', 'typeahead', 'autocomplete', 'react-select', 'MuiInputBase'] 
            for indicator in custom_indicators:
                if indicator in class_name: logger.debug(f"Custom component hinted by class: {indicator}"); return True
            return False
        except: return False
            
    def fill_custom_component(self, element: Locator, value: str, component_type_hint: Optional[str] = None) -> bool:
        logger.info(f"Attempting to fill custom component (value: {value[:20]})")
        try:
            element.scroll_into_view_if_needed(timeout=1000)
            element.click(delay=random.randint(50,100), timeout=DEFAULT_ACTION_TIMEOUT//2) 
            self.page.wait_for_timeout(INTERACTION_DELAY_MS * 2) 
            input_within = element.locator("input[type='text'], input[type='search'], [role='searchbox'], [role='textbox']").first
            target_type_element = element
            if input_within.is_visible(timeout=200):
                logger.debug("Found input within custom component to type into.")
                target_type_element = input_within
            target_type_element.fill(""); target_type_element.type(value, delay=random.randint(40,100))
            self.page.wait_for_timeout(INTERACTION_DELAY_MS * 2) 
            target_type_element.press("Enter")
            logger.info(f"Filled custom component by typing and pressing Enter for '{value[:30]}'.")
            self.page.wait_for_timeout(INTERACTION_DELAY_MS)
            return True
        except Exception as e:
            logger.warning(f"Standard custom component fill failed: {e}. More specific handler might be needed.")
            try:
                element.evaluate("el => el.value = arguments[0]", arg=value)
                element.dispatch_event('change') 
                logger.info("Filled custom component via JS value setting.")
                return True
            except Exception as js_e: logger.error(f"JS value setting also failed for custom component: {js_e}")
        return False

class MultiStepFormHandler: 
    def __init__(self, page: Page, field_detector: FieldDetector, anti_detection: AntiDetectionManager):
        self.page = page; self.field_detector = field_detector; self.anti_detection = anti_detection
    def detect_progress_on_page(self, current_page_or_frame: Union[Page, Frame]) -> Dict[str, Any]:
        return self.field_detector._detect_multi_step_indicators(current_page_or_frame)
    def navigate_next(self, action_buttons_analysis: List[FormField]) -> bool: # Takes FormField list
        if not action_buttons_analysis:
            logger.info("No pre-analyzed 'next' buttons provided to navigate_next. Trying generic selectors.")
            generic_next_selectors = [
                "button:has-text('Next' i)", "button:has-text('Continue' i)", "button:has-text('Proceed' i)",
                "input[type='button'][value*='Next' i]", "input[type='button'][value*='Continue' i]",
                "a:has-text('Next' i)", "a:has-text('Continue' i)",
                "[data-automation-id*='next']", "[data-automation-id*='continue']",
                "[role='button']:has-text('Next' i)", "[role='button']:has-text('Continue' i)",
            ]
            for selector in generic_next_selectors:
                try:
                    elements = self.page.locator(selector).all()
                    for button in elements:
                        if button.is_visible(timeout=500) and button.is_enabled(timeout=500):
                            logger.info(f"Navigating next with generic selector: {selector}")
                            button.scroll_into_view_if_needed(timeout=1000)
                            button.click(delay=random.randint(50,100), timeout=DEFAULT_ACTION_TIMEOUT // 2)
                            self.anti_detection.random_delay(800, 1500) 
                            return True
                except Exception: continue
            logger.warning("Generic 'Next' button not found or failed to click.")
            return False

        for ff_button in action_buttons_analysis: 
            try:
                button_loc = ff_button.element
                if button_loc.is_visible(timeout=500) and button_loc.is_enabled(timeout=500):
                    logger.info(f"Clicking 'Next' button: {ff_button.selector} (Label: {ff_button.label_text})")
                    button_loc.scroll_into_view_if_needed(timeout=1000)
                    button_loc.click(delay=random.randint(50,100), timeout=DEFAULT_ACTION_TIMEOUT // 2)
                    self.anti_detection.random_delay(800, 1500)
                    return True
            except Exception as e:
                logger.warning(f"Error clicking analyzed next button {ff_button.selector}: {e}")
        logger.warning("No suitable 'Next' button found or clicked from analyzed list.")
        return False

class FileUploadHandler: 
    def __init__(self, page: Page): self.page = page
    def handle_file_upload(self, field_match: FormField, file_path_str: str) -> bool:
        try:
            file_path = Path(file_path_str)
            if not file_path.exists() or not file_path.is_file():
                logger.error(f"File not found or is not a file: {file_path_str}")
                return False
            element_loc = field_match.element
            if not (element_loc.is_visible(timeout=1000) and element_loc.is_enabled(timeout=1000)):
                logger.warning(f"File upload field {field_match.selector} not interactable.")
                try:
                    element_loc.evaluate("el => { el.style.display = 'block'; el.style.visibility = 'visible'; el.style.opacity = '1'; }")
                    logger.info("Attempted to make file input visible via JS.")
                    self.page.wait_for_timeout(100) 
                except Exception as js_e: logger.warning(f"Failed to make file input visible via JS: {js_e}")
            element_loc.set_input_files(file_path, timeout=DEFAULT_ACTION_TIMEOUT) 
            logger.info(f"Successfully set input_files for '{field_match.field_type.value}' with: {file_path.name}")
            self.page.wait_for_timeout(INTERACTION_DELAY_MS * 2) 
            return True
        except PlaywrightError as pe: 
            logger.error(f"PlaywrightError uploading file for {field_match.selector}: {pe}")
        except Exception as e:
            logger.error(f"Generic error uploading file {file_path_str} for {field_match.selector}: {e}", exc_info=True)
        return False

# ========== PATTERN LEARNING SYSTEM (Simplified for this integration) ==========
class PatternLearningSystem:
    def __init__(self, storage_path: str = "form_patterns_learned.json"):
        self.storage_path = Path(storage_path)
        logger.info(f"PatternLearningSystem initialized. Storage path: {self.storage_path}")

    def record_attempt(self, form_analysis: FormAnalysisResult, fill_results: FillAttemptResult):
        logger.debug(f"PatternLearningSystem.record_attempt called (placeholder). URL: {form_analysis.url}")
        pass

# ========== UNIVERSAL FORM FILLER ==========
class UniversalFormFiller:
    def __init__(self, page: Page, config: Optional[Dict[str, Any]] = None):
        self.page = page
        self.config = config or self._default_config()
        logger.info(f"UniversalFormFiller initialized. Config loaded.")
        self.field_patterns = self._get_default_field_patterns()
        self.negative_patterns = self._get_default_negative_patterns()
        self.attribute_weights = self._get_default_attribute_weights()
        self.anti_detection = AntiDetectionManager(page)
        self.stability_manager = DOMStabilityManager(page)
        self.field_detector = FieldDetector(page, self.config, self)
        self.shadow_handler = ShadowDOMHandler(page)
        self.custom_component_handler = CustomComponentHandler(page) 
        self.multi_step_handler = MultiStepFormHandler(page, self.field_detector, self.anti_detection)
        self.file_handler = FileUploadHandler(page) 
        self.pattern_learner = PatternLearningSystem()
        self.form_context = FormContextData() 
        self.current_form_analysis: Optional[FormAnalysisResult] = None 
        self.filled_fields_session: set[Tuple[str, FieldType]] = set() 

    @staticmethod
    def _get_default_field_patterns() -> Dict[FieldType, Dict[str, List[str]]]:
        return {
            FieldType.EMAIL: {'names': [r'email', r'e-?mail', r'user-?name', r'login', r'userPrincipalName'],'labels': [r'email\s*address', r'e-?mail', r'your\s*email', r'user\s*name', r'login\s*id'],'placeholders': [r'enter\s*email', r'email', r'@', r'example@company\.com'],'types': [r'^email$'],'autocompletes': [r'email', r'username'],'data-automation-ids': [r'email', r'username', r'userid']},
            FieldType.PASSWORD: {'names': [r'password', r'pass-?word', r'pwd', r'userPass', r'credentials\.password'],'labels': [r'password', r'pass-?word', r'pincode'],'placeholders': [r'enter\s*password', r'password'],'types': [r'^password$'],'autocompletes': [r'current-password', r'new-password'],'data-automation-ids': [r'password']},
            FieldType.FIRST_NAME: {'names': [r'first-?name', r'f-?name', r'given-?name', r'forename', r'firstName', r'contact\.firstName'],'labels': [r'first\s*name', r'given\s*name', r'forename'],'placeholders': [r'first\s*name', r'given\s*name'],'autocompletes': [r'given-name', r'fname']},
            FieldType.LAST_NAME: {'names': [r'last-?name', r'l-?name', r'surname', r'family-?name', r'lastName', r'contact\.lastName'],'labels': [r'last\s*name', r'surname', r'family\s*name'],'placeholders': [r'last\s*name', r'surname'],'autocompletes': [r'family-name', r'lname']},
            FieldType.PHONE: {'names': [r'phone', r'mobile', r'cell', r'telephone', r'contact-?number', r'primaryPhone'],'labels': [r'phone', r'mobile', r'telephone', r'contact\s*number', r'phone\s*number'],'placeholders': [r'phone', r'mobile', r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', r'phone\s*number'],'types': [r'^tel$'],'autocompletes': [r'tel', r'tel-national']},
            FieldType.COMPANY: {'names': [r'company', r'employer', r'organization', r'current-?employer', r'businessName'],'labels': [r'company', r'employer', r'organization', r'current\s*employer', r'company\s*name'],'placeholders': [r'company', r'employer', r'organization\s*name'],'autocompletes': [r'organization']},
            FieldType.JOB_TITLE: {'names': [r'job-?title', r'position', r'role', r'title', r'currentPosition'],'labels': [r'job\s*title', r'position', r'current\s*position', r'role', r'desired\s*position'],'placeholders': [r'job\s*title', r'position', r'your\s*role'],'autocompletes': [r'organization-title']},
            FieldType.RESUME_FILE: {'names': [r'resume', r'cv', r'curriculum', r'resume-?upload', r'cv-?upload', r'attachment'],'labels': [r'resume', r'cv', r'upload\s*resume', r'attach\s*resume', r'curriculum\s*vitae'],'types': [r'^file$'],'data-automation-ids': [r'resumeupload', r'fileuploader', r'attachCV']},
            FieldType.ADDRESS_LINE1: {'names': [r'address(1|Line1)?', r'street', r'addr1'],'labels': [r'address\s*(line\s*1)?', r'street\s*address'],'placeholders': [r'street\s*address', r'address\s*line\s*1'],'autocompletes': [r'address-line1', r'street-address']},
            FieldType.CITY: {'names': [r'city', r'town'],'labels': [r'city', r'town', r'suburb'],'placeholders': [r'city', r'town'],'autocompletes': [r'address-level2', r'city']},
            FieldType.STATE: {'names': [r'state', r'province', r'region'],'labels': [r'state', r'province', r'region'],'placeholders': [r'state', r'province'],'autocompletes': [r'address-level1', r'state']},
            FieldType.ZIP_CODE: {'names': [r'zip(-?code)?', r'postal(-?code)?', r'postcode'],'labels': [r'zip', r'postal\s*code', r'post\s*code'],'placeholders': [r'zip', r'postal\s*code', r'\d{5}(-\d{4})?'],'autocompletes': [r'postal-code', r'zip']},
            FieldType.TEXTAREA: {'names': [r'cover-?letter', r'message', r'comment', r'additional-?info', r'summary', r'description'],'labels': [r'cover\s*letter', r'message', r'comments', r'additional\s*information', r'tell\s*us\s*more']},
        }

    @staticmethod
    def _get_default_negative_patterns() -> Dict[FieldType, List[str]]:
        return {
            FieldType.EMAIL: [r'confirm', r're-?type', r'verify', r'new\s*email', r'search', r'filter'], 
            FieldType.PASSWORD: [r'confirm', r're-?type', r'verify', r'new\s*password', r'current\s*password', r'old\s*password'], 
            FieldType.FIRST_NAME: [r'last', r'family', r'surname', r'middle', r'initial'],
            FieldType.LAST_NAME: [r'first', r'given', r'middle', r'initial'],
            FieldType.PHONE: [r'extension', r'ext\.?', r'country\s*code', r'area\s*code'], 
        }

    @staticmethod
    def _get_default_attribute_weights() -> Dict[str, float]:
        return {'label': 2.5, 'name': 2.2, 'id': 2.0, 'placeholder': 1.8, 'type': 3.0, 'autocomplete': 3.5, 'data-automation-id': 2.8, 'aria-label': 1.5, 'text': 1.0, 'class': 0.5, 'context': 0.3}
        
    def _default_config(self) -> Dict[str, Any]:
        return {
            "form_selectors_priority": ["form[id*='application']", "form[data-testid*='application']", "form[aria-label*='application']","form[id*='job-form']", "form[class*='job-form']","form[id*='signup']", "form[id*='register']","form", "div[role='form']","body"],
            "confidence_threshold": 0.40, "max_retries_interaction": 2, 
            "human_typing_delay_ms": 70, "wait_between_fields_ms": int(INTERACTION_DELAY_MS * 1.5), 
            "enable_anti_detection": True, "enable_learning": False, 
            "dom_stability_timeout_ms": 12000, "dom_stability_window_ms": 1200,  
            "navigation_timeout_ms": DEFAULT_NAVIGATION_TIMEOUT, "action_timeout_ms": DEFAULT_ACTION_TIMEOUT, 
        }

    def _get_current_form_selector(self, current_page_or_frame: Union[Page, Frame]) -> str:
        for selector in self.config.get("form_selectors_priority", ["form", "body"]):
            try:
                if current_page_or_frame.locator(selector).count() > 0:
                    logger.info(f"UFF: Using form context selector: '{selector}' for URL: {current_page_or_frame.url[:70]}")
                    return selector
            except PlaywrightError as e: 
                logger.debug(f"Selector '{selector}' not found or error during form selection: {e}")
            except Exception: pass
        logger.warning("UFF: No primary form selector found from priority list, defaulting to 'body'. This might be slow or too broad.")
        return "body" 

    def _is_element_interactable(self, element: Locator, timeout_ms: int = 500) -> bool:
        try:
            if not element.is_attached(): return False 
            return element.is_visible(timeout=timeout_ms) and element.is_enabled(timeout=timeout_ms)
        except PlaywrightError: return False
        except Exception: return False

    def _get_field_label(self, element_loc: Locator, element_id: Optional[str], aria_labelledby: Optional[str]) -> str:
        if aria_labelledby:
            try:
                label_texts = []
                for label_id in aria_labelledby.split():
                    referenced_label_element = self.page.locator(f"#{re.escape(label_id)}").first
                    if referenced_label_element.is_attached() and referenced_label_element.is_visible(timeout=100):
                        label_texts.append(safe_get_text_content(referenced_label_element).strip())
                if label_texts: return " ".join(label_texts)
            except Exception as e: logger.debug(f"Error getting aria-labelledby content: {e}")

        if element_id:
            try:
                safe_id_selector = re.sub(r'[^a-zA-Z0-9_-]', '', element_id) 
                if safe_id_selector:
                     label_element = self.page.locator(f"label[for='{safe_id_selector}']").first
                     if label_element.is_attached() and label_element.is_visible(timeout=100):
                         return safe_get_text_content(label_element).strip()
            except Exception as e: logger.debug(f"Error getting <label for='{element_id}'>: {e}")
        try:
            parent_label = element_loc.locator("xpath=ancestor::label[1]").first
            if parent_label.is_attached() and parent_label.is_visible(timeout=100):
                return safe_get_text_content(parent_label).strip()
        except Exception as e: logger.debug(f"Error getting parent label text: {e}")
        return "" 

    def _get_surrounding_context(self, element_loc: Locator, max_chars: int = 100) -> Dict[str, str]:
        context = {}
        try:
            parent = element_loc.locator("xpath=parent::*").first
            if parent.is_attached(timeout=100): 
                parent_text = safe_get_text_content(parent)
                context['parent_text'] = parent_text[:max_chars].strip()
            prev_sibling = element_loc.locator("xpath=preceding-sibling::*[1]").first
            if prev_sibling.is_attached(timeout=100):
                context['prev_sibling_text'] = safe_get_text_content(prev_sibling)[:max_chars].strip()
        except PlaywrightError: pass 
        except Exception: pass
        return context

    def _analyze_page_and_detect_fields(self, current_page_or_frame: Union[Page, Frame]) -> FormAnalysisResult:
        self.stability_manager.wait_for_intelligent_stability(
            timeout=self.config["dom_stability_timeout_ms"],
            stability_check_window_ms=self.config["dom_stability_window_ms"]
        )
        form_selector_str = self._get_current_form_selector(current_page_or_frame)
        logger.info(f"UFF: Analyzing page/frame. Effective form selector: '{form_selector_str}'. URL: {current_page_or_frame.url[:100]}...")
        analysis_result = self.field_detector.detect_all_fields_on_page(current_page_or_frame, form_selector_str)
        self.form_context.form_purpose = analysis_result.form_purpose
        self.form_context.is_multi_step = analysis_result.is_multi_step_form
        self.current_form_analysis = analysis_result 
        logger.info(f"UFF: Page analysis complete. Purpose: {analysis_result.form_purpose}, "
                    f"Multi-step: {analysis_result.is_multi_step_form} (Step {analysis_result.current_step_on_page}/{analysis_result.total_steps_on_page}), "
                    f"Fields detected: {len(analysis_result.detected_fields)}.")
        return analysis_result

    def _fill_current_page_fields(self, form_data_for_current_step: Dict[FieldType, Any],
                                 analysis_result: FormAnalysisResult) -> FillAttemptResult:
        fill_result = FillAttemptResult(step_number=self.form_context.current_step) 
        start_time = time.time()
        if not analysis_result or not analysis_result.detected_fields:
            fill_result.status_message = "No fields were detected by analysis on this page/step."
            fill_result.success = False 
            logger.warning(fill_result.status_message)
            fill_result.duration = time.time() - start_time
            return fill_result

        fields_to_attempt_fill: List[Tuple[FormField, Any]] = []         
        for field_type_enum, value_to_fill in form_data_for_current_step.items():
            if not isinstance(field_type_enum, FieldType):
                logger.warning(f"Skipping invalid FieldType in form_data: {field_type_enum}")
                continue
            detected_form_field = analysis_result.detected_fields.get(field_type_enum)
            if detected_form_field:
                fields_to_attempt_fill.append((detected_form_field, value_to_fill))
            else:
                logger.warning(f"Field type {field_type_enum.value} from input data not found among detected fields on page.")
                fill_result.skipped_fields.append(f"{field_type_enum.value} (not detected)")
        
        fill_result.fields_attempted_count = len(fields_to_attempt_fill)
        if not fields_to_attempt_fill:
            fill_result.status_message = "No matching fields to fill based on provided data and page detection."
            fill_result.success = True if not form_data_for_current_step else False 
            fill_result.duration = time.time() - start_time
            return fill_result

        logger.info(f"Attempting to fill {fill_result.fields_attempted_count} fields for step.")
        for form_field_obj, value in fields_to_attempt_fill:
            field_type_str = form_field_obj.field_type.value
            logger.info(f"Filling {field_type_str} (Selector: '{form_field_obj.selector}', Confidence: {form_field_obj.confidence:.2f}) "
                        f"with value: '{str(value)[:50]}{'...' if len(str(value)) > 50 else ''}'")
            if self._fill_single_field(form_field_obj, value):
                fill_result.fields_filled_count += 1
                self.filled_fields_session.add((form_field_obj.selector, form_field_obj.field_type)) 
                if self.config.get("enable_anti_detection", True) and fill_result.fields_filled_count < fill_result.fields_attempted_count:
                    self.anti_detection.random_delay(
                        self.config["wait_between_fields_ms"] - 100,
                        self.config["wait_between_fields_ms"] + 100
                    )
            else:
                logger.warning(f"Failed to fill {field_type_str} (Selector: '{form_field_obj.selector}')")
                fill_result.errors.append(f"Failed to fill {field_type_str} ('{form_field_obj.selector}')")
                fill_result.skipped_fields.append(f"{field_type_str} (fill error)")

        if fill_result.fields_filled_count == fill_result.fields_attempted_count:
            fill_result.success = True
            fill_result.status_message = f"Successfully filled all {fill_result.fields_filled_count} attempted fields."
        elif fill_result.fields_filled_count > 0:
            fill_result.success = "partial"
            fill_result.status_message = f"Partially filled: {fill_result.fields_filled_count}/{fill_result.fields_attempted_count} fields."
        else:
            fill_result.success = False
            fill_result.status_message = "Failed to fill any of the attempted fields."
            if not fill_result.errors and fill_result.skipped_fields : 
                fill_result.status_message = "All fields provided in data were skipped (not detected or fill error)."

        fill_result.duration = time.time() - start_time
        logger.info(f"Field filling for step finished in {fill_result.duration:.2f}s. Status: {fill_result.status_message}")
        return fill_result

    def _fill_single_field(self, field_match: FormField, value: Union[str, bool, Path, int, float]) -> bool:
        element_loc = field_match.element
        field_type = field_match.field_type
        element_html_type = field_match.element_type_html 
        input_type_attr = field_match.attributes.get('type', '').lower() 

        try:
            if not self._is_element_interactable(element_loc, timeout_ms=1000): 
                logger.warning(f"Element for {field_type.value} (Selector: {field_match.selector}) is not interactable before fill.")
                try: element_loc.scroll_into_view_if_needed(timeout=1000)
                except: pass
                if not self._is_element_interactable(element_loc, timeout_ms=200): 
                    return False

            if field_type in [FieldType.RESUME_FILE, FieldType.COVER_LETTER_FILE]:
                if not isinstance(value, (str, Path)):
                    logger.error(f"Invalid value type for file upload ({field_type.value}): {type(value)}. Expected str or Path.")
                    return False
                return self.file_handler.handle_file_upload(field_match, str(value))

            if element_html_type == 'input' and input_type_attr == 'checkbox':
                return self._fill_checkbox(element_loc, bool(value))
            if element_html_type == 'input' and input_type_attr == 'radio':
                if value: return self._fill_radio_button(element_loc) 
                return True 
            if element_html_type == 'select':
                return self._fill_select_dropdown(element_loc, str(value))
            if self.custom_component_handler.is_custom_component(element_loc, field_match.attributes): # Pass attributes
                logger.info(f"Attempting to fill {field_type.value} as a custom component.")
                return self.custom_component_handler.fill_custom_component(element_loc, str(value), component_type_hint=field_type.value)
            if element_html_type in ['input', 'textarea'] or field_match.attributes.get('contenteditable') == 'true':
                return self._fill_standard_text_field(element_loc, str(value), is_content_editable=(field_match.attributes.get('contenteditable') == 'true'))
            
            logger.warning(f"Unhandled element type for filling: HTML Tag='{element_html_type}', Input Type='{input_type_attr}', FieldType='{field_type.value}'")
            return False
        except PlaywrightError as pe:
            logger.error(f"PlaywrightError filling {field_type.value} (Selector: {field_match.selector}): {pe}", exc_info=False)
            return False
        except Exception as e:
            logger.error(f"Unexpected error filling {field_type.value} (Selector: {field_match.selector}): {e}", exc_info=True)
            return False

    def _fill_standard_text_field(self, element: Locator, value: str, is_content_editable: bool = False) -> bool:
        try:
            element.scroll_into_view_if_needed(timeout=1000)
            if self.config.get("enable_anti_detection", True):
                self.anti_detection.human_type(element, value) 
            else:
                element.fill(value, timeout=DEFAULT_ACTION_TIMEOUT // 2) 
            for event_name in ["input", "change", "blur"]:
                try: element.dispatch_event(event_name)
                except PlaywrightError: pass 
            return True
        except PlaywrightError as pe:
            logger.warning(f"PlaywrightError in _fill_standard_text_field: {pe}. Trying fallback type if not contenteditable.")
            if not is_content_editable: 
                 try:
                     element.press("Control+A"); element.press("Delete")    
                     element.type(value, delay=30, timeout=DEFAULT_ACTION_TIMEOUT)
                     for event_name in ["input", "change", "blur"]:
                         try: element.dispatch_event(event_name)
                         except PlaywrightError: pass
                     return True
                 except Exception as type_e:
                     logger.error(f"Fallback type also failed for standard text field: {type_e}")
        except Exception as e:
            logger.error(f"Generic error in _fill_standard_text_field: {e}", exc_info=True)
        return False

    def _fill_checkbox(self, element: Locator, should_be_checked: bool) -> bool:
        try:
            element.scroll_into_view_if_needed(timeout=500)
            current_is_checked = element.is_checked(timeout=DEFAULT_ACTION_TIMEOUT // 3)
            if current_is_checked != should_be_checked:
                element.click(delay=random.randint(30,80), timeout=DEFAULT_ACTION_TIMEOUT // 2)
                self.page.wait_for_timeout(INTERACTION_DELAY_MS // 2) 
                if element.is_checked(timeout=1000) != should_be_checked:
                    logger.warning(f"Checkbox state did not change as expected after click for {generate_robust_selector(element)}")
                    element.set_checked(should_be_checked, timeout=DEFAULT_ACTION_TIMEOUT // 2)
                    if element.is_checked(timeout=1000) != should_be_checked:
                         logger.error(f"Failed to set checkbox state even with set_checked for {generate_robust_selector(element)}")
                         return False
            return True
        except Exception as e:
            logger.error(f"Error filling checkbox {generate_robust_selector(element)}: {e}", exc_info=True)
            return False

    def _fill_radio_button(self, element: Locator) -> bool:
        try:
            element.scroll_into_view_if_needed(timeout=500)
            if not element.is_checked(timeout=DEFAULT_ACTION_TIMEOUT // 3):
                element.check(timeout=DEFAULT_ACTION_TIMEOUT // 2) 
            return True
        except Exception as e:
            logger.error(f"Error filling radio button {generate_robust_selector(element)}: {e}", exc_info=True)
            return False

    def _fill_select_dropdown(self, element: Locator, value_to_select: str) -> bool:
        try:
            element.scroll_into_view_if_needed(timeout=500)
            try:
                element.select_option(value=value_to_select, timeout=DEFAULT_ACTION_TIMEOUT // 3)
                logger.info(f"Selected dropdown option by value: '{value_to_select}'")
                return True
            except PlaywrightError: 
                logger.debug(f"Failed to select dropdown by value '{value_to_select}', trying by label.")
            try:
                element.select_option(label=value_to_select, timeout=DEFAULT_ACTION_TIMEOUT // 3)
                logger.info(f"Selected dropdown option by exact label: '{value_to_select}'")
                return True
            except PlaywrightError:
                logger.debug(f"Failed to select dropdown by exact label '{value_to_select}', trying by partial label.")
            try:
                options = element.locator("option").all()
                for opt_loc in options:
                    opt_text = (safe_get_text_content(opt_loc) or "").strip()
                    opt_val = safe_get_attribute(opt_loc, "value")
                    if value_to_select.lower() in opt_text.lower():
                        target_to_select = {"label": opt_text} if not opt_val else {"value": opt_val}
                        element.select_option(**target_to_select, timeout=DEFAULT_ACTION_TIMEOUT // 3)
                        logger.info(f"Selected dropdown option by partial label match ('{value_to_select}' in '{opt_text}') using {target_to_select}")
                        return True
                logger.warning(f"No option found in dropdown for partial label match: '{value_to_select}'")
                return False 
            except Exception as e_partial: 
                logger.error(f"Error during partial label match for dropdown: {e_partial}")
                return False
        except Exception as e: 
            logger.error(f"Error filling select dropdown {generate_robust_selector(element)} for value '{value_to_select}': {e}", exc_info=True)
            return False

    def _attempt_to_submit_or_navigate(self, analysis_result: FormAnalysisResult, is_last_data_step: bool) -> bool:
        action_taken = False
        button_type_to_click = 'submit' if is_last_data_step else 'next'
        logger.info(f"Attempting to '{button_type_to_click}' for {'last step' if is_last_data_step else 'current step'}.")
        action_buttons: List[FormField] = analysis_result.action_buttons.get(button_type_to_click, [])
        
        if is_last_data_step and button_type_to_click == 'submit' and not action_buttons:
             logger.info("No specific 'submit' button found on last step, checking for 'apply' buttons from analysis.")
             action_buttons.extend(analysis_result.action_buttons.get('apply', []))
             if action_buttons: button_type_to_click = 'apply' 

        if action_buttons:
            for ff_button in action_buttons:
                try:
                    button_loc = ff_button.element
                    if self._is_element_interactable(button_loc, timeout_ms=1000):
                        btn_text = ff_button.label_text or ff_button.selector
                        logger.info(f"Clicking analyzed '{button_type_to_click}' button: '{btn_text}' (Selector: {ff_button.selector})")
                        button_loc.scroll_into_view_if_needed(timeout=1000)
                        button_loc.click(delay=random.randint(50,120), timeout=DEFAULT_ACTION_TIMEOUT)
                        action_taken = True; break 
                except Exception as e:
                    logger.warning(f"Error clicking analyzed '{button_type_to_click}' button ({ff_button.selector}): {e}")
            if action_taken: return True

        logger.warning(f"No suitable '{button_type_to_click}' button found from prior analysis. Trying fallback generic navigation.")
        if not is_last_data_step: 
            if self.multi_step_handler.navigate_next(analysis_result.action_buttons.get('next', [])): 
                logger.info("Successfully navigated to next step using MultiStepFormHandler with analyzed buttons.")
                return True
            elif self.multi_step_handler.navigate_next([]): 
                 logger.info("Successfully navigated to next step using generic MultiStepFormHandler.")
                 return True
            else: logger.warning("MultiStepFormHandler also failed to find/click a 'next' button.")
        else: 
            if self._submit_form_fallback():
                logger.info("Successfully submitted form using generic fallback submit.")
                return True
            else: logger.warning("Generic fallback submit also failed.")
        return False

    def _submit_form_fallback(self) -> bool:
        submit_selectors = [
            "button[type='submit']:visible", "input[type='submit']:visible",
            "button:has-text('Submit' i):visible", "button:has-text('Apply' i):visible", 
            "button:has-text('Finish' i):visible", "button:has-text('Complete' i):visible",
            "[data-automation-id*='submit']:visible", "[data-automation-id*='finish']:visible",
            "[role='button']:has-text('Submit' i):visible"
        ]
        for selector in submit_selectors:
            try:
                elements = self.page.locator(selector).all()
                for button in elements: 
                    if self._is_element_interactable(button, timeout_ms=200):
                        btn_text = safe_get_text_content(button).strip()
                        logger.info(f"Fallback: Clicking generic submit button: '{btn_text}' (Selector: {selector})")
                        button.scroll_into_view_if_needed(timeout=500)
                        button.click(delay=random.randint(50,120), timeout=DEFAULT_ACTION_TIMEOUT)
                        return True
            except Exception: continue 
        return False

    def fill_entire_application(self, form_data_steps: List[Dict[FieldType, Any]], initial_url: str) -> OverallApplicationResult:
        overall_results = OverallApplicationResult(application_url=initial_url, total_steps_provided_in_data=len(form_data_steps))
        
        decision_handler = DecisionHandler() 

        logger.info(f"UFF: Starting full application fill for {initial_url}. Total data steps: {len(form_data_steps)}")
        
        try:
            self.page.goto(initial_url, wait_until="load", timeout=self.config["navigation_timeout_ms"])
            logger.info(f"Navigation to {initial_url} complete. Current URL: {self.page.url}")
            if self.config["enable_anti_detection"]: self.anti_detection.random_delay(700, 1500)

            page_after_initial_goto_url = self.page.url.lower()
            is_on_job_listing = ("job" in page_after_initial_goto_url or "career" in page_after_initial_goto_url or "search/job" in page_after_initial_goto_url) and \
                                not any(x in page_after_initial_goto_url for x in ["apply", "application", "candidate", "form", "talent", "login", "signin"])

            if is_on_job_listing:
                logger.info("Detected job listing page, looking for Apply button...")
                if handle_job_listing_page(self.page): 
                    logger.info("Successfully clicked Apply on job listing. Waiting for next page...")
                    self.stability_manager.wait_for_intelligent_stability(timeout=15000, stability_check_window_ms=1500)
                    logger.info(f"Page appears stable after Apply click. Current URL: {self.page.url}")
                    
                    logger.info("Checking for decision points using DecisionHandler...")
                    decision_was_handled_by_handler = False # Renamed for clarity
                    max_decision_attempts = 3 
                                
                    for attempt in range(max_decision_attempts): 
                        if check_and_handle_decision_points(self.page, decision_handler): 
                            logger.info("DecisionHandler: Successfully handled decision point this attempt.")
                            decision_was_handled_by_handler = True                                        
                            try:
                                self.page.wait_for_load_state("networkidle", timeout=10000)
                            except PlaywrightTimeoutError: 
                                logger.warning("DecisionHandler: Timeout waiting for networkidle after handling decision.")
                            except Exception as e_load: 
                                logger.warning(f"DecisionHandler: Error during wait_for_load_state: {e_load}")
                            self.page.wait_for_timeout(2000) 
                            break 
                        else:
                            page_text_content_lower = ""
                            try:
                                page_text_content_lower = self.page.content(timeout=1000).lower() 
                            except PlaywrightTimeoutError:
                                logger.warning("Timeout getting page content during decision retry check.")
                            except Exception as e_page_content:
                                logger.warning(f"Error getting page content during decision retry check: {e_page_content}")

                            if "start your application" in page_text_content_lower or "please select how you would like to apply" in page_text_content_lower:
                                logger.warning(f"DecisionHandler: Still on decision page (or similar text found), attempt {attempt + 1}/{max_decision_attempts}")
                                self.page.wait_for_timeout(2000) 
                            else:
                                logger.info("DecisionHandler: No longer on a recognizable decision page or decision point not found in this attempt.")
                                break 
                                
                    if not decision_was_handled_by_handler:
                        page_text_after_attempts_lower = ""
                        try:
                            page_text_after_attempts_lower = self.page.content(timeout=1000).lower()
                        except: pass # Ignore errors here, just trying to get text for condition

                        if "start your application" in page_text_after_attempts_lower or "please select how you would like to apply" in page_text_after_attempts_lower:
                            logger.warning("DecisionHandler: Could not automatically handle decision point after multiple attempts.")
                            logger.info("Please manually select an option in the browser, then press Enter here...")
                            input("Press Enter after making your selection in the browser...") 
                            logger.info("Resuming after manual intervention for decision point.")
                            self.stability_manager.wait_for_intelligent_stability(timeout=10000, stability_check_window_ms=1000)
                        else:
                            logger.info("DecisionHandler: No specific decision page text found to warrant manual prompt; proceeding.")
                    
                    logger.info(f"Exited decision handling stage. Current URL: {self.page.url}")
                    self.stability_manager.wait_for_intelligent_stability(timeout=10000, stability_check_window_ms=1000) 
                else:
                    logger.warning("Could not find/click Apply button on job listing. Proceeding with current page as is.")
                    try: self.page.screenshot(path="debug_job_listing_no_apply.png", full_page=True)
                    except: pass
            else: logger.info("Initial page does not seem like a job listing, or already on an application/login page. Skipping 'Apply button' hunt.")

            current_url_for_login_check = self.page.url.lower()
            is_on_login_page_indicator = any(ind in current_url_for_login_check for ind in ["signin", "login", "auth", "sso", "accountlogin"])
            
            if is_on_login_page_indicator:
                logger.info("Login page indicators detected. Attempting to log in.")
                email, password = "", ""
                if form_data_steps and isinstance(form_data_steps[0], dict):
                    email = form_data_steps[0].get(FieldType.EMAIL, "")
                    password = form_data_steps[0].get(FieldType.PASSWORD, "")
                
                if email and password:
                    if handle_login_page(self.page, email, password): 
                        logger.info("Login attempt appears successful!")
                        self.stability_manager.wait_for_intelligent_stability(timeout=15000, stability_check_window_ms=1500)
                        logger.info(f"Page appears stable after login. Current URL: {self.page.url}")
                    else:
                        logger.error("Login attempt failed based on handle_login_page outcome.")
                        overall_results.errors.append("Login failed.")
                        overall_results.final_status = "fail_login"
                        if check_for_create_account_option(self.page): 
                            overall_results.errors.append("Account creation might be required.")
                        try: self.page.screenshot(path="debug_login_failed.png", full_page=True)
                        except: pass
                        return overall_results 
                elif is_on_login_page_indicator : 
                    logger.warning("On login page but no email/password provided in form_data_steps[0].")
                    overall_results.errors.append("Login page, but no credentials given for automated login.")
            else: logger.info("Not immediately identified as a login page, or no credentials to attempt login. Proceeding.")

            if not wait_and_handle_captcha(self.page): 
                logger.warning("CAPTCHA detected. Manual intervention may be required. Script will continue but might fail.")
                overall_results.errors.append("CAPTCHA challenge encountered.")
            close_popups_comprehensive(self.page) 

            for i, current_step_data in enumerate(form_data_steps, 1):
                overall_results.steps_attempted_on_site = i
                self.form_context.current_step = i 
                self.filled_fields_session.clear() 
                logger.info(f"\n{'='*20} UFF: PROCESSING FORM STEP {i}/{len(form_data_steps)} (URL: {self.page.url[:100]}) {'='*20}")
                current_page_analysis = self._analyze_page_and_detect_fields(self.page)
                if not current_page_analysis: 
                    msg = f"Step {i}: Critical error - page analysis returned None (should not happen)."
                    logger.error(msg); overall_results.errors.append(msg)
                    overall_results.final_status = f"fail_S{i}_analysis_null"; break
                if current_page_analysis.errors:
                    overall_results.errors.extend([f"S{i} Analysis Err: {e}" for e in current_page_analysis.errors])

                if not current_page_analysis.detected_fields and not any(btns for btns in current_page_analysis.action_buttons.values()):
                    logger.warning(f"Step {i}: No data fields or action buttons detected by analysis. This might be an interstitial page or end of automated flow.")
                    if i < len(form_data_steps):
                        msg = f"Step {i} (not last): No fields or actions. Possible dead-end or misinterpretation."
                        logger.error(msg); overall_results.errors.append(msg)
                        overall_results.final_status = f"fail_S{i}_no_fields_actions"
                        try: self.page.screenshot(path=f"debug_S{i}_no_fields_actions.png", full_page=True)
                        except: pass; break 
                    else: logger.info(f"Step {i} (last data step): No fields or actions. Assuming this might be a final confirmation page.")

                step_fill_result = self._fill_current_page_fields(current_step_data, current_page_analysis)
                overall_results.step_details.append(step_fill_result)
                overall_results.total_fields_filled_across_steps += step_fill_result.fields_filled_count
                if step_fill_result.errors:
                    overall_results.errors.extend([f"S{i} Fill Err: {e}" for e in step_fill_result.errors])

                step_ok_to_proceed = step_fill_result.success is True or \
                                     step_fill_result.success == "partial" or \
                                     (step_fill_result.fields_attempted_count == 0 and not current_step_data) 
                if not step_ok_to_proceed:
                    msg = f"Step {i} field filling was not successful: {step_fill_result.status_message}. Stopping."
                    logger.error(msg); overall_results.final_status = f"fail_S{i}_fill"
                    try: self.page.screenshot(path=f"debug_S{i}_fill_fail.png", full_page=True)
                    except: pass; break
                
                overall_results.steps_successfully_filled +=1
                is_last_data_step_provided = (i == len(form_data_steps))
                site_seems_to_have_next_step = any(current_page_analysis.action_buttons.get('next',[])) or \
                                            current_page_analysis.is_multi_step_form and \
                                            current_page_analysis.current_step_on_page < current_page_analysis.total_steps_on_page
                effective_last_step_on_site = is_last_data_step_provided and not site_seems_to_have_next_step
                logger.info(f"Step {i}: Is last data step provided: {is_last_data_step_provided}. Site seems to have next step: {site_seems_to_have_next_step}. Effective last site step: {effective_last_step_on_site}")
                navigation_or_submit_success = self._attempt_to_submit_or_navigate(current_page_analysis, effective_last_step_on_site)
                
                if not navigation_or_submit_success:
                    action_str = 'submit final form' if effective_last_step_on_site else f'navigate from step {i}'
                    msg = f"Failed to {action_str}."
                    logger.error(msg); overall_results.errors.append(msg)
                    overall_results.final_status = f"fail_S{i}_{'submit' if effective_last_step_on_site else 'nav'}"
                    try: self.page.screenshot(path=f"debug_S{i}_nav_submit_fail.png", full_page=True)
                    except: pass; break 

                if effective_last_step_on_site:
                    logger.info("Final submission/navigation initiated for application.")
                    overall_results.final_status = "submission_attempted"
                    self.stability_manager.wait_for_intelligent_stability(timeout=15000, stability_check_window_ms=1500)
                    logger.info(f"Page appears stable after final submission attempt. Final URL: {self.page.url}"); break 
                else: 
                    logger.info(f"Successfully navigated from step {i}. Waiting for next page to load/stabilize...")
                    self.stability_manager.wait_for_intelligent_stability(timeout=self.config["navigation_timeout_ms"], stability_check_window_ms=1500)
                    logger.info(f"Page appears stable for next step. New URL: {self.page.url}")
                    if self.config["enable_anti_detection"]: self.anti_detection.random_delay(800, 1800)
                    if not wait_and_handle_captcha(self.page):
                         logger.warning(f"CAPTCHA detected on step {i+1}. Manual intervention may be required.")
                         overall_results.errors.append(f"CAPTCHA on step {i+1}")
                    close_popups_comprehensive(self.page)

            if overall_results.final_status == "initiated": 
                if overall_results.steps_successfully_filled == overall_results.total_steps_provided_in_data:
                    overall_results.final_status = "completed_all_data_steps"
                    logger.info("All provided data steps were processed. Final submission state might need verification (e.g. 'submission_attempted').")
                else:
                    overall_results.final_status = f"ended_after_S{overall_results.steps_attempted_on_site}_incomplete"
                    logger.warning(f"Process ended after step {overall_results.steps_attempted_on_site} but not all data steps were successfully completed.")
        except PlaywrightError as pe: 
            logger.critical(f"UFF: Playwright Critical failure: {pe}", exc_info=True)
            overall_results.errors.append(f"Playwright Critical: {str(pe)}"); overall_results.final_status = "error_playwright_critical"
        except Exception as e:
            logger.critical(f"UFF: General Critical failure in fill_entire_application: {e}", exc_info=True)
            overall_results.errors.append(f"General Critical: {str(e)}"); overall_results.final_status = "error_general_critical"
        finally:
            if "fail" in overall_results.final_status or "error" in overall_results.final_status:
                 if hasattr(self, 'page') and self.page and not self.page.is_closed():
                    try:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        self.page.screenshot(path=f"debug_UFF_final_state_{timestamp}.png", full_page=True)
                        logger.info(f"Saved final state screenshot: debug_UFF_final_state_{timestamp}.png")
                    except Exception as ss_final_err: logger.error(f"Failed to save final error screenshot: {ss_final_err}")
            overall_results.timestamp = time.time() 
        return overall_results

# ========== SETUP AND UTILITIES (Mostly from original) ==========
def setup_stealth_browser() -> Tuple[Optional[Page], Optional[BrowserContext], Optional[Browser], Optional[Any]]: 
    from playwright.sync_api import sync_playwright
    playwright_instance, browser_instance, browser_context_instance = None, None, None
    try:
        playwright_instance = sync_playwright().start()
        browser_instance = playwright_instance.chromium.launch(
            headless=False, 
            args=['--no-sandbox','--disable-dev-shm-usage','--disable-blink-features=AutomationControlled','--disable-gpu','--disable-extensions','--disable-plugins-discovery','--start-maximized'])
        browser_context_instance = browser_instance.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 
            viewport={'width': 1366, 'height': 768}, java_script_enabled=True, bypass_csp=True, 
            extra_http_headers={'Accept-Language': 'en-US,en;q=0.9', 'DNT': '1'})
        page = browser_context_instance.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']})")
        page.add_init_script("Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5].map(i => ({name: `Plugin ${i}`, filename: `plugin${i}.dll`}))})")
        page.add_init_script("Object.defineProperty(Notification, 'permission', {get: () => 'denied'})") 
        logger.info("Stealth browser setup completed.")
        return page, browser_context_instance, browser_instance, playwright_instance 
    except Exception as e:
        logger.error(f"Error setting up stealth browser: {e}", exc_info=True)
        if browser_context_instance:
            try:
                browser_context_instance.close()
            except Exception as e_close_ctx:
                logger.warning(f"Exception during browser_context_instance.close() on error: {e_close_ctx}")
        if browser_instance and browser_instance.is_connected():
            try:
                browser_instance.close()
            except Exception as e_close_browser:
                logger.warning(f"Exception during browser_instance.close() on error: {e_close_browser}")
        if playwright_instance:
            try:
                playwright_instance.stop()
            except Exception as e_stop_pw:
                logger.warning(f"Exception during playwright_instance.stop() on error: {e_stop_pw}")
        return None, None, None, None


def wait_and_handle_captcha(page: Page, timeout_sec: int = 7) -> bool: 
    logger.info("Checking for CAPTCHA elements...")
    captcha_selectors = [
        "iframe[src*='recaptcha']", "iframe[title*='reCAPTCHA']", "[class*='g-recaptcha']",
        "iframe[src*='hcaptcha']", "iframe[title*='hcaptcha']", "[class*='h-captcha']",
        "iframe[src*='arkoselabs.com']", "iframe[title*='arkose']", "[class*='arkose']", 
        "div#turnstile-widget", "iframe[src*='challenges.cloudflare.com']", 
        "[data-testid='captcha']", "[aria-label*='captcha' i]"
    ]
    page.wait_for_timeout(500)
    found_captcha = False
    for selector in captcha_selectors:
        try:
            elements = page.locator(selector)
            if elements.count() > 0:
                for i in range(elements.count()):
                    el = elements.nth(i)
                    if el.is_visible(timeout=500): 
                        logger.warning(f"CAPTCHA detected with selector: {selector} (Element: {safe_get_attribute(el,'title') or safe_get_tag_name(el)})")
                        found_captcha = True; break
            if found_captcha: break
        except PlaywrightTimeoutError: logger.debug(f"Captcha selector {selector} not visible in time.")
        except Exception as e: 
            logger.debug(f"Error checking CAPTCHA selector {selector}: {e}")
            if page.is_closed(): return True 
    if found_captcha:
        logger.info(f"CAPTCHA present. Please solve it manually in the browser window within {timeout_sec} seconds.")
        page.wait_for_timeout(timeout_sec * 1000) 
        logger.info("Resuming after CAPTCHA wait. Hopefully it was solved.")
        return False 
    logger.info("No obvious CAPTCHA elements detected.")
    return True 

def close_popups_comprehensive(page: Page) -> None:
    logger.info("Checking for common popups/modals/cookie banners to close...")
    popup_close_actions = [
        ("button:has-text('Accept all cookies' i)", "Accept All Cookies"),("button:has-text('Allow Cookies' i)", "Allow Cookies"),
        ("button:has-text('Got it' i)", "Got It (Cookie)"),("button:has-text('I accept' i)", "I Accept (Cookie)"),
        ("button[id*='cookie'][id*='accept']", "Cookie Accept by ID"),("[aria-label*='accept cookie' i]", "Cookie Accept by Aria"),
        ("button[aria-label*='close' i]", "Close by Aria"),("button[aria-label*='dismiss' i]", "Dismiss by Aria"),
        ("button:has-text('×')", "Close with × symbol"), ("button:has-text('X')", "Close with X char"),
        ("button[class*='close' i][class*='button' i]", "Close by Class"),("button[id*='close' i]", "Close by ID"),
        ("span[aria-label*='close' i]", "Close Span by Aria"), 
        ("button:has-text('No thanks' i)", "No Thanks (Survey)"),("button:has-text('Maybe later' i)", "Maybe Later (Survey)"),
        ("button:has-text('Don\\'t allow' i)", "Don't Allow (Notification)"), 
    ]
    max_popups_to_close_in_one_go = 3; closed_count = 0
    for _ in range(max_popups_to_close_in_one_go): 
        popup_closed_this_iteration = False
        for selector, description in popup_close_actions:
            try:
                elements = page.locator(selector).all()
                for element in elements:
                    if element.is_visible(timeout=200) and element.is_enabled(timeout=200):
                        logger.info(f"Attempting to close popup: '{description}' with selector: {selector}")
                        element.click(delay=random.randint(30,80), timeout=1000) 
                        page.wait_for_timeout(INTERACTION_DELAY_MS) 
                        logger.info(f"Clicked '{description}' popup/banner element.")
                        popup_closed_this_iteration = True; closed_count += 1; break 
            except PlaywrightTimeoutError: logger.debug(f"Popup action '{description}' ({selector}) not completed in time or element not interactable.")
            except PlaywrightError as pe: logger.debug(f"PlaywrightError with popup action '{description}' ({selector}): {pe}")
            except Exception as e: logger.debug(f"Generic error with popup action '{description}' ({selector}): {e}")
            if popup_closed_this_iteration and closed_count < max_popups_to_close_in_one_go :
                 page.wait_for_timeout(INTERACTION_DELAY_MS*2); break 
        if not popup_closed_this_iteration: break 
    if closed_count > 0: logger.info(f"Closed {closed_count} popup(s)/banner(s) in total.")
    else: logger.info("No common popups/banners found or closed.")

# ========== MAIN EXECUTION ==========
def main():
    logger.info("Script started. Initializing UniversalFormFiller...")
    page, browser_context, browser_instance, playwright_instance = setup_stealth_browser()
    
    if not page: 
        logger.critical("Failed to initialize browser. Exiting.")
        return
        
    applicant_data_step1 = {
        FieldType.EMAIL: f"test.user.{random.randint(10000, 99999)}@example.com", 
        FieldType.PASSWORD: "VerySecureP@ss123!",  
        FieldType.FIRST_NAME: "Playwright", FieldType.LAST_NAME: "TestUser",
        FieldType.PHONE: f"555-555-{random.randint(1000, 9999):04d}",
        FieldType.ADDRESS_LINE1: f"{random.randint(100,9999)} Main St",
        FieldType.CITY: "Anytown", FieldType.STATE: "CA", 
        FieldType.ZIP_CODE: f"{random.randint(10000,99999)}"
    }
    resume_dir = Path(__file__).resolve().parent
    resume_file_path = resume_dir / "dummy_resume.pdf"
    if not resume_file_path.exists():
        try:
            with open(resume_file_path, "wb") as f:
                f.write(b"%PDF-1.0\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj 3 0 obj<</Type/Page/MediaBox[0 0 612 792]>>endobj\nxref\n0 4\n0000000000 65535 f\n0000000010 00000 n\n0000000056 00000 n\n0000000115 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n164\n%%EOF")
            logger.info(f"Created dummy resume: {resume_file_path}")
        except IOError as e: logger.error(f"Could not create dummy resume at {resume_file_path}: {e}")
    if resume_file_path.exists(): applicant_data_step1[FieldType.RESUME_FILE] = str(resume_file_path) 
    form_data_all_steps = [applicant_data_step1]
    filler = UniversalFormFiller(page) 
    overall_results: Optional[OverallApplicationResult] = None
    target_job_url = JOB_FORM_URL_EXAMPLE # You can change this to the Truist URL directly for testing
    # target_job_url = "https://careers.truist.com/us/en/job/TBJTBFUSR0100804EXTERNALENUS/Data-Scientist-3"

    
    try:
        overall_results = filler.fill_entire_application(form_data_all_steps, target_job_url)
        logger.info(f"\n{'='*20} FINAL APPLICATION OVERALL RESULTS {'='*20}")
        def default_serializer(obj):
            if isinstance(obj, Locator): 
                try: return f"<Locator for '{generate_robust_selector(obj)}'>"
                except: return "<Locator object (not easily serializable)>"
            if isinstance(obj, Path): return str(obj)
            if isinstance(obj, Enum): return obj.value
            if hasattr(obj, '__dict__') and not isinstance(obj, (type, Callable)):
                return {k: v for k, v in vars(obj).items() if k != 'element'} 
            try: return str(obj) 
            except Exception: return f"<Object of type {type(obj).__name__} not serializable>"
        results_json_str = json.dumps(overall_results, indent=2, default=default_serializer)
        logger.info(results_json_str)
        logger.info(f"{'='*60}")
        
        if overall_results: 
            final_status = overall_results.final_status
            if not (final_status == "likely_submitted" or final_status == "submission_attempted" or \
                    final_status == "completed_all_data_steps" or final_status == "completed_all_steps"): 
                logger.warning(f"Application process ended with status: '{final_status}'. Review logs and screenshots.")
                logger.info("\nPossible issues or next steps to check:\n1. Was the 'Apply' button on the job listing correctly handled?\n2. Did a 'post-apply' choice screen appear that was not handled (e.g., 'Autofill with resume')? Ensure decision_handler.py is effective or provide manual input if prompted.\n3. Was a login required and did it succeed with provided credentials?\n4. Did a CAPTCHA appear that wasn't solved?\n5. Were all form fields correctly detected and filled on each step?\n6. Did the navigation between steps or final submission work?\n7. Check any 'debug_*.png' screenshots in the script's directory.")
            else: logger.info(f"Application process completed with status: '{final_status}'.")

        browser_still_active = False
        if browser_instance and browser_instance.is_connected():
            if browser_context: 
                 if page and not page.is_closed(): 
                     browser_still_active = True
        
        if browser_still_active and not filler.config.get("headless", False): 
            input("\nReview browser (if open) and press Enter to close and end script...")
        else:
            logger.info("Browser already closed or in headless mode. Script will end.")
            
    except Exception as e_main:
        logger.critical(f"Critical unhandled exception in main execution block: {e_main}", exc_info=True)
        if page and not page.is_closed():
            try:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                page.screenshot(path=f"critical_error_main_final_{timestamp}.png", full_page=True)
                logger.info(f"Saved screenshot for critical main error: critical_error_main_final_{timestamp}.png")
            except: pass 
            
    finally:
        logger.info("Initiating cleanup: closing browser and Playwright...")
        
        if browser_context: 
            try:
                browser_context.close() 
                logger.debug("Attempted to close browser context.")
            except Exception as e_ctx_close:
                logger.warning(f"Exception during browser_context.close(): {e_ctx_close}")

        if browser_instance: 
            try:
                if browser_instance.is_connected():
                    browser_instance.close()
                    logger.debug("Browser instance closed.")
            except Exception as e_browser_close:
                logger.warning(f"Error closing browser instance: {e_browser_close}")

        if playwright_instance: 
            try:
                playwright_instance.stop() 
                logger.info("Playwright instance stopped.")
            except Exception as e_pw_stop:
                logger.warning(f"Error stopping Playwright instance: {e_pw_stop}")

if __name__ == "__main__":
    main()
