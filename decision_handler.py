#!/usr/bin/env python3
"""
Decision Handler System for Form Filler
Handles decision points like application method selection with stored preferences
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any 
from playwright.sync_api import Page, Locator, TimeoutError # CORRECTED IMPORT
import time
import re 

logger = logging.getLogger(__name__)

class DecisionPoint:
    """Represents a decision point in the application flow"""
    def __init__(self, name: str, description: str, detection_criteria: Dict[str, Any], options: List[Dict[str, Any]]):
        self.name = name
        self.description = description
        self.detection_criteria = detection_criteria
        self.options = options

class DecisionHandler:
    """Handles decision points with stored preferences"""
    
    def __init__(self, preferences_file: str = "form_filler_preferences.json"):
        self.preferences_file = Path(preferences_file)
        self.preferences = self._load_preferences()
        custom_definitions = self.preferences.get("custom_decision_definitions", [])
        initialized_points = self._initialize_decision_points()
        existing_names = {dp.name for dp in initialized_points}
        for custom_def in custom_definitions:
            if custom_def.get("name") not in existing_names:
                try:
                    initialized_points.append(DecisionPoint(**custom_def)) 
                    existing_names.add(custom_def.get("name"))
                except TypeError as e:
                    logger.error(f"Error loading custom decision definition '{custom_def.get('name')}': {e}. Ensure keys match DecisionPoint constructor.")
        self.decision_points = initialized_points
        
    def _load_preferences(self) -> Dict[str, Any]:
        if self.preferences_file.exists():
            try:
                with open(self.preferences_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError: 
                logger.error(f"Error decoding JSON from {self.preferences_file}. File might be corrupted. Initializing fresh preferences.")
            except Exception as e:
                logger.error(f"Error loading preferences from {self.preferences_file}: {e}. Initializing fresh preferences.")
        return {"decisions": {}, "custom_decision_definitions": []} 
    
    def _save_preferences(self):
        try:
            with open(self.preferences_file, 'w', encoding='utf-8') as f:
                json.dump(self.preferences, f, indent=2)
            logger.info(f"Preferences saved to {self.preferences_file}")
        except Exception as e:
            logger.error(f"Error saving preferences: {e}")
    
    def _initialize_decision_points(self) -> List[DecisionPoint]:
        return [
            DecisionPoint(
                name="workday_application_method_selection", 
                description="Workday: Choose how to apply (Autofill/Manual/Last Application)",
                detection_criteria={
                    "url_patterns": ["myworkdayjobs.com"], 
                    "text_indicators": ["Start Your Application", "Please select how you would like to apply", "How would you like to apply?"],
                    "button_options_texts": ["Autofill with Resume", "Apply Manually", "Use My Last Application"] 
                },
                options=[
                    {
                        "name": "autofill_resume_workday", 
                        "selectors": [
                            "a[data-automation-id='autofillWithResume']",      
                            "button[data-automation-id='autofillWithResume']", 
                            "button:text-matches('(?i)autofill.*resume')",
                            "a:text-matches('(?i)autofill.*resume')",
                            "[aria-label*='Autofill with Resume' i]"
                        ],
                        "preferred": True 
                    },
                    {
                        "name": "apply_manually_workday",
                        "selectors": [
                            "a[data-automation-id='applyManually']", 
                            "button[data-automation-id='applyManually']",
                            "button:has-text('Apply Manually')",
                            "a:has-text('Apply Manually')",
                            "[aria-label*='Apply Manually' i]"
                        ],
                        "preferred": False
                    },
                    {
                        "name": "use_last_application_workday",
                        "selectors": [
                            "a[data-automation-id='useMyLastApplication']", 
                            "button[data-automation-id='useMyLastApplication']",
                            "button:has-text('Use My Last Application')",
                            "a:has-text('Use My Last Application')",
                            "[aria-label*='Use My Last Application' i]"
                        ],
                        "preferred": False
                    }
                ]
            ),
            DecisionPoint(
                name="resume_parse_confirmation",
                description="Confirm parsed resume information after autofill",
                detection_criteria={
                    "url_patterns": ["myworkdayjobs.com/apply"], 
                    "text_indicators": ["Review your information", "Confirm your details", "Information from resume", "Review and Submit", "My Information"],
                    "button_options_texts": ["Continue", "Next", "Save and continue", "Edit Information"]
                },
                options=[
                    {
                        "name": "continue_after_parse", 
                        "selectors": [
                            "button[data-automation-id*='continue' i]", 
                            "button[data-automation-id*='next' i]",
                            "button:has-text('Continue' i)", 
                            "button:has-text('Next' i)",
                            "button:text-matches('(?i)save.*continue')"
                            ],
                        "preferred": True
                    }
                ]
            )
        ]
    
    def detect_decision_point(self, page: Page) -> Optional[DecisionPoint]:
        try:
            current_url = page.url.lower()
            logger.debug(f"Detecting decision point for URL: {current_url}")

            for dp_idx, decision_point in enumerate(self.decision_points):
                criteria = decision_point.detection_criteria
                logger.debug(f"Checking DecisionPoint {dp_idx}: {decision_point.name}")

                url_match = any(pattern.lower() in current_url for pattern in criteria.get("url_patterns", []))
                
                text_match = False
                text_indicators = criteria.get("text_indicators", [])
                if text_indicators:
                    for indicator_text in text_indicators:
                        try:
                            if page.locator(f"*:text-matches('{re.escape(indicator_text)}', 'i')").count() > 0:
                                text_match = True
                                logger.debug(f"  Text indicator '{indicator_text}' found for {decision_point.name}.")
                                break
                        except Exception as e_loc:
                            logger.debug(f"  Error with text indicator locator for '{indicator_text}': {e_loc}")
                
                if not text_match and text_indicators: 
                    try:
                        page_content_lower = page.content(timeout=2000).lower() 
                        text_match = any(indicator.lower() in page_content_lower for indicator in text_indicators)
                        if text_match: logger.debug(f"  Text indicator found via page.content() for {decision_point.name}.")
                    except TimeoutError: # USE CORRECTED EXCEPTION NAME
                        logger.warning("Timeout getting page.content() in detect_decision_point")
                    except Exception as e_content:
                        logger.warning(f"Error getting page.content() in detect_decision_point: {e_content}")

                button_options_texts = criteria.get("button_options_texts", [])
                actual_buttons_found_count = 0
                if button_options_texts:
                    for btn_txt in button_options_texts:
                        try:
                            if page.locator(f"button:has-text('{re.escape(btn_txt)}' i), a[role='button']:has-text('{re.escape(btn_txt)}' i), [data-automation-id*='button']:has-text('{re.escape(btn_txt)}' i)").count() > 0:
                                actual_buttons_found_count += 1
                        except Exception as e_btn_loc:
                             logger.debug(f"  Error with button option locator for '{btn_txt}': {e_btn_loc}")
                
                strong_indicator_match = url_match or text_match
                buttons_criterion_met = True 
                if button_options_texts:
                    required_buttons = max(1, len(button_options_texts) // 2) 
                    buttons_criterion_met = actual_buttons_found_count >= required_buttons
                    logger.debug(f"  For {decision_point.name}: URLMatch={url_match}, TextMatch={text_match}, ButtonsFound={actual_buttons_found_count}/{len(button_options_texts)}, CriterionMet={buttons_criterion_met}")

                if strong_indicator_match and buttons_criterion_met:
                    logger.info(f"Detected decision point: {decision_point.name}")
                    return decision_point
                            
        except Exception as e:
            logger.error(f"Error during detect_decision_point: {e}", exc_info=True)
        return None
    
    def get_stored_decision(self, decision_name: str) -> Optional[str]:
        return self.preferences.get("decisions", {}).get(decision_name)
    
    def store_decision(self, decision_name: str, choice_name: str):
        if "decisions" not in self.preferences:
            self.preferences["decisions"] = {}
        self.preferences["decisions"][decision_name] = choice_name
        self._save_preferences()
    
    def handle_decision_point(self, page: Page, decision_point: DecisionPoint, auto_select: bool = True) -> bool:
        try:
            logger.info(f"Handling decision point: {decision_point.name}")
            stored_choice_name = self.get_stored_decision(decision_point.name)
            selected_option_definition = None

            if stored_choice_name:
                for option_def in decision_point.options:
                    if option_def["name"] == stored_choice_name:
                        selected_option_definition = option_def
                        break
                if selected_option_definition:
                    logger.info(f"Using stored preference: '{stored_choice_name}' for decision '{decision_point.name}'")
                else:
                    logger.warning(f"Stored preference '{stored_choice_name}' for '{decision_point.name}' not found in current options. Will try default.")

            if not selected_option_definition and auto_select:
                for option_def in decision_point.options:
                    if option_def.get("preferred", False):
                        selected_option_definition = option_def
                        break
                if selected_option_definition:
                    logger.info(f"Using default preferred option: '{selected_option_definition['name']}' for '{decision_point.name}'")
            
            if not selected_option_definition:
                logger.warning(f"No stored or default preferred option to select for '{decision_point.name}'. Manual intervention likely needed if script doesn't proceed.")
                try:
                    page.screenshot(path=f"debug_decision_point_no_pref_{decision_point.name}.png", full_page=True)
                except Exception as e_ss: logger.error(f"Failed to save no_pref screenshot: {e_ss}")
                return False 
            
            clicked_successfully = False
            for selector in selected_option_definition["selectors"]:
                try:
                    elements = page.locator(selector).all()
                    for element in elements:
                        if element.is_visible(timeout=1000) and element.is_enabled(timeout=1000): 
                            logger.info(f"Clicking option: '{selected_option_definition['name']}' with selector: {selector}")
                            element.click(timeout=3000) 
                            self.store_decision(decision_point.name, selected_option_definition["name"])
                            page.wait_for_timeout(3000) # Increased wait after click
                            clicked_successfully = True
                            return True 
                except TimeoutError: # USE CORRECTED EXCEPTION NAME
                    logger.debug(f"Option selector '{selector}' for '{selected_option_definition['name']}' not interactable or click timed out.")
                except Exception as e:
                    logger.debug(f"Failed attempt with selector '{selector}' for option '{selected_option_definition['name']}': {e}")
            
            if not clicked_successfully:
                logger.warning(f"Could not click any selector for preferred/stored option: '{selected_option_definition['name']}' in decision '{decision_point.name}'")
                try:
                    page.screenshot(path=f"debug_decision_click_failed_{decision_point.name}_{selected_option_definition['name']}.png", full_page=True)
                except Exception as e_ss: logger.error(f"Failed to save click_failed screenshot: {e_ss}")
            return False
            
        except Exception as e:
            logger.error(f"Error handling decision point '{decision_point.name}': {e}", exc_info=True)
            return False
    
    def add_custom_decision_point(self, name: str, description: str, 
                                 detection_criteria: Dict[str, Any], 
                                 options: List[Dict[str, Any]]):
        new_decision = DecisionPoint(name, description, detection_criteria, options)
        if not any(dp.name == name for dp in self.decision_points):
            self.decision_points.append(new_decision)
            logger.info(f"Added custom decision point to current session: {name}")
        else:
            logger.info(f"Custom decision point '{name}' already exists in session. Updating if definition differs (not implemented yet).")

        custom_defs = self.preferences.get("custom_decision_definitions", [])
        custom_defs = [d for d in custom_defs if d.get("name") != name]
        custom_defs.append({
            "name": name, "description": description,
            "detection_criteria": detection_criteria, "options": options
        })
        self.preferences["custom_decision_definitions"] = custom_defs
        self._save_preferences()
        logger.info(f"Saved/Updated definition for custom decision point: {name}")
    
    def interactive_decision_setup(self, page: Page) -> Optional[str]:
        logger.info("\n=== INTERACTIVE DECISION SETUP (Placeholder) ===")
        logger.info("Script encountered an unrecognized situation that might be a new decision point.")
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"new_unrecognized_decision_{timestamp}.png"
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            logger.info(f"Screenshot of unrecognized page saved: {screenshot_path}")
        except Exception as e: logger.error(f"Failed to save screenshot for new decision point: {e}")
        
        buttons_texts = []
        try:
            candidate_elements = page.locator("button:visible, a[role='button']:visible, [data-automation-id*='button']:visible, [data-automation-id*='action']:visible").all()
            for el_idx, btn_el in enumerate(candidate_elements):
                if el_idx > 15: break 
                text = (btn_el.text_content() or btn_el.get_attribute("aria-label") or btn_el.get_attribute("data-automation-id") or f"Element_{el_idx}").strip()
                if text and len(text) < 70 : 
                    buttons_texts.append(text)
        except Exception as e_btn: logger.warning(f"Could not list buttons for interactive setup: {e_btn}")

        if buttons_texts:
            logger.info("\nSome detected clickable elements on the current page:")
            for i, btn_text in enumerate(buttons_texts): logger.info(f"  {i+1}. {btn_text}")
        else: logger.info("No obvious clickable button/link texts detected for quick suggestion.")
            
        logger.info("\nThis version does not support fully interactive setup of NEW decision points via this method.")
        logger.info("Please analyze the page (see screenshot) and consider adding a new DecisionPoint definition to decision_handler.py or using add_custom_decision_point programmatically.")
        logger.info("For now, you might need to manually interact with the browser to proceed past this unrecognized state if the script is stuck.")
        return None

def handle_application_method_selection(page: Page) -> bool:
    try:
        logger.info("Attempting specific handler: handle_application_method_selection for Workday-like 'Apply Options' page")
        text_indicators_present = False
        indicators = ["Start Your Application", "Please select how you would like to apply", "How would you like to apply?"]
        for indicator_text in indicators:
            try: 
                if page.locator(f"*:text-matches('{re.escape(indicator_text)}', 'i')").count() > 0: # Use re.escape
                    text_indicators_present = True
                    logger.debug(f"Specific handler: Text indicator '{indicator_text}' found.")
                    break
            except Exception as e_loc_text:
                logger.debug(f"Error checking text indicator '{indicator_text}': {e_loc_text}")

        if not text_indicators_present:
            logger.debug("Specific handler: Application method selection page indicators not strongly detected by locators. Will not proceed with this specific handler.")
            return False 

        logger.info("Specific handler: Application method selection page detected by text indicators.")
        autofill_option_selectors = [
            "a[data-automation-id='autofillWithResume']",      
            "button[data-automation-id='autofillWithResume']", 
            "a[data-automation-id='autoFillResumeButton']", 
            "button[data-automation-id='autoFillResumeButton']",
            "button:text-matches('(?i)autofill.*resume')", 
            "a:text-matches('(?i)autofill.*resume')",     
            "[aria-label*='Autofill with Resume' i]",
        ]
        
        for selector in autofill_option_selectors:
            try:
                elements = page.locator(selector).all() 
                for element in elements:
                    if element.is_visible(timeout=1000) and element.is_enabled(timeout=1000):
                        logger.info(f"Specific handler: Found 'Autofill with Resume' option with selector: {selector}")
                        element.click(timeout=3000)
                        logger.info("Specific handler: Clicked 'Autofill with Resume'")
                        page.wait_for_timeout(3000) 
                        return True 
            except TimeoutError: # USE CORRECTED EXCEPTION NAME
                 logger.debug(f"Specific handler: Selector '{selector}' for Autofill not visible/enabled in time.")
            except Exception as e:
                logger.debug(f"Specific handler: Error with selector '{selector}' for Autofill: {e}")
        
        logger.warning("Specific handler: Could not find or click 'Autofill with Resume' after trying all selectors.")
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            page.screenshot(path=f"decision_autofill_fail_{timestamp}.png", full_page=True)
            logger.info(f"Screenshot saved: decision_autofill_fail_{timestamp}.png")
        except Exception as e_ss: logger.error(f"Failed to save decision_autofill_fail screenshot: {e_ss}")
        return False 
        
    except Exception as e:
        logger.error(f"Error in handle_application_method_selection: {e}", exc_info=True)
        return False

def check_and_handle_decision_points(page: Page, decision_handler_instance: DecisionHandler) -> bool: 
    try:
        if handle_application_method_selection(page): 
            logger.info("Application method selection handled by specific function.")
            return True
                
        logger.info("No specific handler acted or was applicable for current page. Trying general DecisionHandler detection.")
        decision_point_obj = decision_handler_instance.detect_decision_point(page) 
        if decision_point_obj:
            logger.info(f"General DecisionHandler detected: {decision_point_obj.name}")
            if decision_handler_instance.handle_decision_point(page, decision_point_obj):
                logger.info(f"General DecisionHandler successfully handled: {decision_point_obj.name}")
                return True
            else:
                logger.warning(f"General DecisionHandler detected '{decision_point_obj.name}' but could not resolve it automatically.")
                return False 
        
        logger.info("No known decision points detected by general DecisionHandler or specific handlers.")
        return False 
            
    except Exception as e:
        logger.error(f"Critical error in check_and_handle_decision_points: {e}", exc_info=True)
        return False
