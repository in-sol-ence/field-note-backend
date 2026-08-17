import asyncio
import logging
import json
import os
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any
import urllib.parse
from playwright.async_api import async_playwright, Page, Response, Browser, BrowserContext
from .exceptions import PageLoadError
from .decorators import retry_on_network_error

try:
    import jmespath  # type: ignore
except ImportError:
    jmespath = None  # type: ignore


class PlaywrightScraper:
    def __init__(self, username: str, password: str, email: str, scraping_config: Dict, timeout_config: Dict, proxy_config: Optional[Dict] = None, progress_manager=None):
        self.username = username
        self.password = password
        self.email = email
        self.proxy_config = proxy_config
        self.progress_manager = progress_manager
        self.logger = logging.getLogger(__name__)
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None 
        self.scraped_tweet_ids = set()
        self.all_tweets = []
        self.user_data = None
        self.captured_requests = []
        self.cookies_file = "playwright_cookies.json"
        self.is_logged_in = False     
        self.current_username = None
        self.start_time: Optional[float] = None
        self.scroll_delay_min = scraping_config['scroll_delay_min']
        self.scroll_delay_max = scraping_config['scroll_delay_max']
        self.max_scroll_attempts = scraping_config['max_scroll_attempts']
        self.scroll_attempts_without_new = 0
        self.max_attempts_without_new = scraping_config['max_attempts_without_new']
        self.max_tweets_per_session = None
        self.overlap_threshold = scraping_config['overlap_detection_threshold']
        self.timeouts = timeout_config
        
        self.logger.info("Playwright scraper initialized")
    
    async def initialize(self): 
        try:
            self.playwright = await async_playwright().start()
            
            # Silent by default. Set X_SCRAPER_HEADED=1 only for manual login /
            # cookie refresh when the window must be visible.
            self.headless = os.environ.get("X_SCRAPER_HEADED", "").strip().lower() not in {
                "1",
                "true",
                "yes",
            }
            browser_args = {
                'headless': self.headless,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                ]
            }
            self.logger.info(
                "Playwright chromium launch headless=%s", self.headless
            )
            
            if self.proxy_config and self.proxy_config.get('enable_proxy_rotation'):
                proxy_list = self.proxy_config.get('proxies', [])
                if proxy_list:
                    proxy_str = proxy_list[0]
                    parts = proxy_str.split(':')
                    if len(parts) == 4:
                        host, port, username, password = parts
                        browser_args['proxy'] = {
                            'server': f'http://{host}:{port}',
                            'username': username,
                            'password': password
                        }
                        self.logger.info(f"Using proxy: {username}@{host}:{port}")
                        self.logger.info("Note: First connection through proxy may take 30-60 seconds...")
            
            self.browser = await self.playwright.chromium.launch(**browser_args)
            
            self.context = await self.browser.new_context(
                viewport={'width': 1280, 'height': 900},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/Chicago'
            )
            # Reduce obvious automation signals X uses to block sign-in.
            await self.context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            
            cookies_loaded = False
            if Path(self.cookies_file).exists():
                try:
                    cookies_data = json.loads(Path(self.cookies_file).read_text())
                    if cookies_data:  
                        await self.context.add_cookies(cookies_data)
                        self.logger.info("Loaded saved cookies - will skip login")
                        self.is_logged_in = True 
                        cookies_loaded = True
                except Exception as e:
                    self.logger.warning(f"Failed to load cookies: {e}")
            
            if not cookies_loaded:
                self.logger.info("No saved cookies found - will need to login")
            
            self.page = await self.context.new_page()
            
            
            self.page.on("response", self._intercept_response)
            
            self.logger.info("Playwright browser initialized successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to initialize Playwright: {e}")
            return False
    
    async def _intercept_response(self, response: Response):
        try:
            
            if response.request.resource_type in ["xhr", "fetch"]:
                url = response.url
                
                if 'graphql' in url.lower() or 'api.twitter.com' in url or 'api.x.com' in url:
                    if '/' in url:
                        parts = url.split('/')
                        for i, part in enumerate(parts):
                            if 'graphql' in part.lower() and i + 1 < len(parts):
                                operation = parts[i + 1].split('?')[0]
                                self.logger.debug(f"GraphQL: {operation}")
                                break
                
                
                if any(endpoint in url for endpoint in [
                    'UserByScreenName',
                    'UserTweets',
                    'TweetDetail',
                    'TweetResultByRestId',
                    'SearchTimeline',
                    'SearchAdaptive'
                ]):
                    try:
                        data = await response.json()
                        self.captured_requests.append({
                            'url': url,
                            'data': data,
                            'timestamp': time.time()
                        })
                        
                        
                        if 'UserByScreenName' in url:
                            self.logger.info(f"Parsing UserByScreenName response ({url.split('graphql')[-1][:80]})")
                            print("[scrape] got UserByScreenName API response", flush=True)
                            self._parse_user_data(data)
                        elif 'UserTweets' in url:
                            self.logger.info("Parsing UserTweets response")
                            print("[scrape] got UserTweets API response", flush=True)
                            before = len(self.all_tweets)
                            self._parse_tweets_from_timeline(data)
                            print(f"[scrape] UserTweets parse: +{len(self.all_tweets)-before} tweets", flush=True)
                        elif 'SearchTimeline' in url or 'SearchAdaptive' in url:
                            self.logger.info("Parsing Search response")
                            print("[scrape] got Search API response", flush=True)
                            self._parse_tweets_from_timeline(data)
                        elif 'TweetResultByRestId' in url or 'TweetDetail' in url:
                            self.logger.info("Parsing TweetDetail response")
                            self._parse_single_tweet(data)
                        else:
                            # Log other GraphQL ops so we can see renamed endpoints
                            for part in url.split('/'):
                                if part and part[0].isupper() and 'queryId' not in part:
                                    self.logger.info(f"Unhandled GraphQL op: {part.split('?')[0]}")
                                    print(f"[scrape] unhandled GraphQL: {part.split('?')[0]}", flush=True)
                                    break
                            
                    except Exception as e:
                        self.logger.warning(f"Failed to parse response from {url[:100]}: {e}")
                        
        except Exception as e:
            self.logger.debug(f"Error in response interceptor: {e}")
    
    def _parse_user_data(self, data: Dict):
        if not jmespath:
            self.logger.warning("jmespath not available, skipping user data parsing")
            return
            
        try:
            
            user_result = jmespath.search('data.user.result', data)
            if user_result:
                legacy = user_result.get('legacy', {})
                self.user_data = {
                    'id': user_result.get('rest_id', ''),
                    'username': legacy.get('screen_name', ''),
                    'display_name': legacy.get('name', ''),
                    'bio': legacy.get('description', ''),
                    'followers_count': legacy.get('followers_count', 0),
                    'following_count': legacy.get('friends_count', 0),
                    'tweet_count': legacy.get('statuses_count', 0),
                    'verified': user_result.get('is_blue_verified', False) or legacy.get('verified', False),
                    'profile_image_url': legacy.get('profile_image_url_https', ''),
                    'profile_banner_url': legacy.get('profile_banner_url', ''),
                    'created_at': legacy.get('created_at', ''),
                    'location': legacy.get('location', ''),
                    'url': legacy.get('url', ''),
                }
                self.logger.info(f"Captured user data: @{self.user_data['username']} ({self.user_data['followers_count']} followers)")
        except Exception as e:
            self.logger.error(f"Error parsing user data: {e}")
    
    def _parse_tweets_from_timeline(self, data: Dict):
        if not jmespath:
            self.logger.warning("jmespath not available, skipping tweet parsing")
            return
            
        try:
            self.logger.debug(f"Parsing timeline data structure...")
            
            instructions = jmespath.search('data.user.result.timeline_v2.timeline.instructions', data)
            if not instructions:
                instructions = jmespath.search('data.user.result.timeline.timeline.instructions', data)
            if not instructions:
                instructions = jmespath.search('data.search_by_raw_query.search_timeline.timeline.instructions', data)
            if not instructions:
                instructions = jmespath.search('data.threaded_conversation_with_injections_v2.instructions', data)
            
            if not instructions:
                self.logger.warning("No timeline instructions found in any known format")
                data_keys = list(data.get('data', {}).keys()) if isinstance(data.get('data'), dict) else []
                self.logger.warning(f"Available data keys: {data_keys}")
                print(f"[scrape] WARN: no timeline instructions; data.keys={data_keys}", flush=True)
                return
            
            self.logger.debug(f"Found {len(instructions)} instructions")
            
            for instruction in instructions:
                instruction_type = instruction.get('type')
                self.logger.debug(f"Processing instruction type: {instruction_type}")
                
                if instruction_type == 'TimelineAddEntries':
                    entries = instruction.get('entries', [])
                    self.logger.info(f"Found {len(entries)} entries in timeline")
                    
                    tweet_count = 0
                    skipped_entries = []
                    all_entry_ids = []
                    for entry in entries:
                        entry_id = entry.get('entryId', '')
                        all_entry_ids.append(entry_id)
                        
                        if any(skip_type in entry_id for skip_type in ['cursor-', 'who-to-follow', 'profile-conversation']):
                            skipped_entries.append(entry_id)
                            continue
                        
                        tweet_result = jmespath.search('content.itemContent.tweet_results.result', entry)
                        if tweet_result:
                            parsed_tweet = self._extract_tweet_data(tweet_result)
                            tweet_id = parsed_tweet.get('id') if parsed_tweet else None
                            if parsed_tweet and tweet_id:
                                if tweet_id not in self.scraped_tweet_ids:
                                    if not hasattr(self, 'existing_tweet_ids') or tweet_id not in self.existing_tweet_ids:
                                        self.all_tweets.append(parsed_tweet)
                                        self.scraped_tweet_ids.add(tweet_id)
                                        tweet_count += 1
                    
                    if tweet_count > 0:
                        self.logger.info(f"Extracted {tweet_count} tweets from this batch")
                    else:
                        self.logger.warning(f"No tweets extracted from {len(entries)} entries")
                        if all_entry_ids:
                            self.logger.debug(f"All entry IDs: {all_entry_ids[:10]}")
                        if skipped_entries:
                            self.logger.debug(f"Skipped entry IDs: {skipped_entries[:5]}")  
                                
        except Exception as e:
            self.logger.error(f"Error parsing timeline tweets: {e}", exc_info=True)
    
    def _parse_single_tweet(self, data: Dict):
        if not jmespath:
            return
            
        try:
            tweet_result = jmespath.search('data.tweetResult.result', data)
            if tweet_result:
                parsed_tweet = self._extract_tweet_data(tweet_result)
                tweet_id = parsed_tweet.get('id') if parsed_tweet else None
                
                if parsed_tweet and tweet_id and tweet_id not in self.scraped_tweet_ids:
                    if not hasattr(self, 'existing_tweet_ids') or tweet_id not in self.existing_tweet_ids:
                        self.all_tweets.append(parsed_tweet)
                        self.scraped_tweet_ids.add(tweet_id)
        except Exception as e:
            self.logger.error(f"Error parsing single tweet: {e}")
    
    def _extract_tweet_data(self, tweet_result: Dict) -> Optional[Dict[str, Any]]:
        try:
            
            if tweet_result.get('__typename') == 'TweetWithVisibilityResults':
                tweet_result = tweet_result.get('tweet', {})
            
            legacy = tweet_result.get('legacy', {})
            tweet_id = tweet_result.get('rest_id', '')
            
            
            user_result = tweet_result.get('core', {}).get('user_results', {}).get('result', {})
            user_legacy = user_result.get('legacy', {})
            
            
            media = []
            extended_entities = legacy.get('extended_entities', {})
            for media_item in extended_entities.get('media', []):
                media_info = {
                    'type': media_item.get('type', ''),
                    'url': media_item.get('media_url_https', ''),
                    'expanded_url': media_item.get('expanded_url', '')
                }
                if media_item.get('type') == 'video':
                    variants = media_item.get('video_info', {}).get('variants', [])
                    
                    video_variants = [v for v in variants if v.get('content_type') == 'video/mp4']
                    if video_variants:
                        media_info['video_url'] = max(video_variants, key=lambda x: x.get('bitrate', 0))['url']
                media.append(media_info)
            
            
            urls = []
            for url_entity in legacy.get('entities', {}).get('urls', []):
                urls.append({
                    'url': url_entity.get('url', ''),
                    'expanded_url': url_entity.get('expanded_url', ''),
                    'display_url': url_entity.get('display_url', '')
                })
            
            hashtags = [ht.get('text', '') for ht in legacy.get('entities', {}).get('hashtags', [])]
            
            
            tweet_data = {
                'id': tweet_id,
                'text': legacy.get('full_text', ''),
                'full_text': legacy.get('full_text', ''),
                'created_at': legacy.get('created_at', ''),
                'user': {
                    'id': user_result.get('rest_id', ''),
                    'username': user_legacy.get('screen_name', ''),
                    'display_name': user_legacy.get('name', ''),
                    'followers_count': user_legacy.get('followers_count', 0),
                    'following_count': user_legacy.get('friends_count', 0),
                    'verified': user_result.get('is_blue_verified', False) or user_legacy.get('verified', False),
                    'profile_image_url': user_legacy.get('profile_image_url_https', ''),
                    'description': user_legacy.get('description', '')
                },
                'metrics': {
                    'retweet_count': legacy.get('retweet_count', 0),
                    'favorite_count': legacy.get('favorite_count', 0),
                    'reply_count': legacy.get('reply_count', 0),
                    'quote_count': legacy.get('quote_count', 0),
                    'view_count': tweet_result.get('views', {}).get('count', 0)
                },
                'lang': legacy.get('lang', 'en'),
                'possibly_sensitive': legacy.get('possibly_sensitive', False),
                'is_retweet': legacy.get('retweeted', False),
                'is_reply': legacy.get('in_reply_to_status_id_str') is not None,
                'is_quote': legacy.get('is_quote_status', False),
                'hashtags': hashtags,
                'urls': urls,
                'media': media,
                'scraped_at': time.time()
            }
            
            return tweet_data
            
        except Exception as e:
            self.logger.debug(f"Error extracting tweet data: {e}")
            return None

    async def _wait_for_first_selector(self, selectors, timeout: int = 30000):
        """Return the first visible element matching any selector, or None."""
        if not self.page:
            return None
        deadline = time.time() + (timeout / 1000.0)
        last_error = None
        while time.time() < deadline:
            for selector in selectors:
                try:
                    element = await self.page.query_selector(selector)
                    if element and await element.is_visible():
                        return element
                except Exception as e:
                    last_error = e
            await asyncio.sleep(0.25)
        if last_error:
            self.logger.debug(f"Selector wait ended with: {last_error}")
        return None

    async def _type_like_human(self, element, text: str) -> None:
        await element.click()
        await element.fill('')
        for char in text:
            await element.type(char, delay=random.randint(40, 120))

    async def _save_session_cookies(self) -> None:
        if not self.context:
            return
        cookies = await self.context.cookies()
        Path(self.cookies_file).write_text(json.dumps(cookies, indent=2))
        self.logger.info(f"Saved {len(cookies)} cookies to {self.cookies_file}")

    async def _is_on_home_feed(self) -> bool:
        if not self.page:
            return False
        url = (self.page.url or '').lower()
        if any(token in url for token in ('login', 'flow', 'onboarding', 'i/jf')):
            return False
        try:
            compose = await self.page.query_selector('[data-testid="SideNav_NewTweet_Button"]')
            if compose and await compose.is_visible():
                return True
        except Exception:
            pass
        return '/home' in url

    async def _manual_login_fallback(self) -> bool:
        """Let the user finish X login in the open browser, then save cookies."""
        if not self.page or not self.context:
            return False

        if getattr(self, "headless", True):
            self.logger.error(
                "Manual X login needs a visible browser. Re-run once with "
                "X_SCRAPER_HEADED=1 to sign in and save cookies, then scrapes "
                "stay headless."
            )
            return False

        self.logger.warning(
            "X blocked automated sign-in. Complete login manually in the browser "
            "(password, email/phone challenge, captcha, etc.)."
        )
        try:
            if 'login' not in (self.page.url or '').lower() and 'flow' not in (self.page.url or '').lower():
                await self.page.goto(
                    'https://x.com/i/flow/login',
                    wait_until='domcontentloaded',
                    timeout=self.timeouts['page_load_timeout'],
                )
        except Exception as e:
            self.logger.warning(f"Could not reload login page for manual flow: {e}")

        print("\n" + "=" * 60)
        print("Manual login required")
        print("1. In the browser window, finish signing in to X.")
        print("2. Wait until you see your home timeline.")
        print("3. Come back here and press ENTER.")
        print("=" * 60 + "\n")
        await asyncio.get_event_loop().run_in_executor(None, input)

        try:
            await self.page.goto(
                'https://x.com/home',
                wait_until='domcontentloaded',
                timeout=self.timeouts['page_load_timeout'],
            )
            await asyncio.sleep(3)
        except Exception as e:
            self.logger.warning(f"Home navigation after manual login failed: {e}")

        if await self._is_on_home_feed():
            self.is_logged_in = True
            await self._save_session_cookies()
            self.logger.info("Manual login succeeded; session cookies saved")
            return True

        self.logger.error(f"Manual login not confirmed (url={self.page.url})")
        try:
            await self.page.screenshot(path="login_failure.png")
            self.logger.info("Screenshot saved to login_failure.png")
        except Exception:
            pass
        return False
    
    async def login(self) -> bool:
        if not self.page or not self.context:
            raise RuntimeError("Browser not initialized")
        
        try:
            
            if self.is_logged_in:
                self.logger.info("Already logged in with saved cookies - skipping login")
                
                try:
                    await self.page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=self.timeouts['element_wait_timeout'])
                    
                    self.logger.info("Verifying cookies... (waiting for page to fully load)")
                    try:
                        await self.page.wait_for_selector('[data-testid="SideNav_NewTweet_Button"]', timeout=self.timeouts['cookie_verification_timeout'])
                        self.logger.info("✓ Cookie login verified successfully")
                        return True
                    except:
                        current_url = self.page.url
                        if 'login' not in current_url and 'flow' not in current_url:
                            self.logger.info("✓ Cookies valid (on home page)")
                            return True
                        else:
                            self.logger.warning("Cookies expired, need to login again")
                            self.is_logged_in = False
                except Exception as e:
                    self.logger.warning(f"Cookie verification failed: {e}, will login")
                    self.is_logged_in = False
            
            self.logger.info("Attempting to login to Twitter (may take 30-60s through proxy)...")
            
            
            self.logger.info("Loading login page...")
            # Prefer x.com; may land on classic flow or newer onboarding UI.
            await self.page.goto('https://x.com/i/flow/login', 
                               wait_until='domcontentloaded', 
                               timeout=self.timeouts['page_load_timeout']) 
            
            
            await asyncio.sleep(self.timeouts['post_login_page_delay'])
            
            try:
                self.logger.info("Waiting for username input field...")
                username_input = await self._wait_for_first_selector([
                    'input[autocomplete="username"]',
                    'input[name="text"]',
                    'input[autocomplete="email"]',
                    'input[type="email"]',
                    'input[type="text"]',
                ], timeout=self.timeouts['element_wait_timeout'])
                if not username_input:
                    raise Exception(f"Username input field not found (url={self.page.url})")
                self.logger.info(f"Username field found on {self.page.url}, entering credentials...")
                await self._type_like_human(username_input, self.username)
                await asyncio.sleep(self.timeouts['post_input_delay'])
                
                
                self.logger.info("Advancing past username step...")
                next_button = await self._wait_for_first_selector([
                    'button:has-text("Next")',
                    'button:has-text("Continue")',
                    '[role="button"]:has-text("Next")',
                    '[role="button"]:has-text("Continue")',
                ], timeout=self.timeouts['button_click_timeout'])
                if next_button:
                    await next_button.click()
                else:
                    await username_input.press('Enter')
                await asyncio.sleep(self.timeouts['post_click_delay'])
            except Exception as e:
                self.logger.error(f"Failed to enter username: {e}")
                try:
                    await self.page.screenshot(path="login_error_username.png")
                    self.logger.info("Error screenshot saved: login_error_username.png")
                except:
                    pass
                return await self._manual_login_fallback()
            
            
            try:
                
                self.logger.info("Checking for email verification...")
                password_visible = await self._wait_for_first_selector([
                    'input[name="password"]',
                    'input[type="password"]',
                ], timeout=1500)
                if not password_visible:
                    email_input = await self._wait_for_first_selector([
                        'input[data-testid="ocfEnterTextTextInput"]',
                        'input[name="text"]',
                    ], timeout=self.timeouts['short_wait_timeout'])
                    if email_input:
                        self.logger.info("Email/username verification required")
                        await self._type_like_human(email_input, self.email or self.username)
                        await asyncio.sleep(self.timeouts['post_input_delay'])
                        next_button = await self._wait_for_first_selector([
                            'button:has-text("Next")',
                            'button:has-text("Continue")',
                        ], timeout=self.timeouts['button_click_timeout'])
                        if next_button:
                            await next_button.click()
                        else:
                            await email_input.press('Enter')
                        await asyncio.sleep(self.timeouts['post_click_delay'])
                else:
                    self.logger.info("No email verification needed")
            except:
                self.logger.info("No email verification needed")
                pass  
            
            try:
                self.logger.info("Waiting for password input field...")
                password_input = await self._wait_for_first_selector([
                    'input[name="password"]',
                    'input[type="password"]',
                    'input[autocomplete="current-password"]',
                ], timeout=self.timeouts['element_wait_timeout'])
                if not password_input:
                    raise Exception("Password input field not found")
                self.logger.info("Password field found, entering password...")
                await self._type_like_human(password_input, self.password)
                await asyncio.sleep(self.timeouts['post_input_delay'])
                
                
                self.logger.info("Clicking Login button...")
                login_button = await self._wait_for_first_selector([
                    'button[data-testid="LoginForm_Login_Button"]',
                    'button:has-text("Log in")',
                    'button:has-text("Sign in")',
                    '[role="button"]:has-text("Log in")',
                ], timeout=self.timeouts['button_click_timeout'])
                if login_button:
                    await login_button.click()
                else:
                    await password_input.press('Enter')
                self.logger.info("Waiting for login to complete...")
                await asyncio.sleep(self.timeouts['login_wait_delay'])
            except Exception as e:
                self.logger.error(f"Failed to enter password: {e}")
                try:
                    await self.page.screenshot(path="login_error_password.png")
                    self.logger.info("Error screenshot saved: login_error_password.png")
                except:
                    pass
                return await self._manual_login_fallback()
            
            
            try:
                await self.page.wait_for_url('**/home**', timeout=self.timeouts['login_complete_timeout'])
                self.is_logged_in = True
                await self._save_session_cookies()
                self.logger.info("Successfully logged in to Twitter")
                return True
            except:
                
                current_url = self.page.url
                self.logger.info(f"Current URL after login attempt: {current_url}")

                # Common X block page after automated credential entry
                try:
                    body_text = (await self.page.inner_text('body')).lower()
                except Exception:
                    body_text = ''
                if any(msg in body_text for msg in (
                    "couldn't log you in",
                    "couldn’t log you in",
                    "failed to finish signing in",
                    "something went wrong",
                )):
                    self.logger.error("X rejected automated sign-in")
                    return await self._manual_login_fallback()
                

                if await self._is_on_home_feed():
                    self.is_logged_in = True
                    await self._save_session_cookies()
                    self.logger.info("Successfully logged in to Twitter")
                    return True

                if any(indicator in current_url.lower() for indicator in ['home', 'x.com', 'twitter.com']):
                    if any(x in current_url.lower() for x in ['login', 'flow', 'onboarding']):
                        self.logger.error(f"Login failed - still on auth URL: {current_url}")
                        return await self._manual_login_fallback()

                    self.logger.warning("Logged in but couldn't verify home page - proceeding anyway")
                    self.is_logged_in = True
                    await self._save_session_cookies()
                    return True
                else:
                    self.logger.error(f"Login failed - on URL: {current_url}")
                    return await self._manual_login_fallback()
                    
        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return await self._manual_login_fallback()
    
    async def scrape_user_tweets(self, username: str, resume_from_tweet_id: Optional[str] = None, max_tweets_per_session: Optional[int] = None, existing_tweet_ids: Optional[set] = None) -> Dict[str, Any]:
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        try:
            self.current_username = username
            self.start_time = time.time()
            self.scraped_tweet_ids.clear()
            self.all_tweets.clear()
            self.user_data = None
            self.max_tweets_per_session = max_tweets_per_session
            self.existing_tweet_ids = existing_tweet_ids or set()  
            
            if resume_from_tweet_id:
                limit_info = f" (limit: {max_tweets_per_session} tweets)" if max_tweets_per_session else " (unlimited)"
                self.logger.info(f"Resuming scrape for @{username} from tweet {resume_from_tweet_id}{limit_info}")
                self.logger.info(f"   Tracking {len(self.existing_tweet_ids)} existing tweet IDs to avoid duplicates")
            else:
                limit_info = f" (limit: {max_tweets_per_session} tweets)" if max_tweets_per_session else " (unlimited)"
                self.logger.info(f"Starting scrape for @{username}{limit_info}")
            
            
            profile_url = f'https://x.com/{username}'
            await self.page.goto(profile_url, 
                               wait_until='domcontentloaded',
                               timeout=self.timeouts['page_load_timeout'])
            await asyncio.sleep(self.timeouts['post_navigation_delay'])
            
            
            try:
                error_element = await self.page.query_selector('text="This account doesn\'t exist"')
                if error_element:
                    self.logger.error(f"Account @{username} doesn't exist")
                    return {'error': 'Account not found', 'tweets': []}
            except:
                pass
            
            
            try:
                await self.page.wait_for_selector('article[data-testid="tweet"], [data-testid="tweet"]', timeout=self.timeouts['button_click_timeout'])
            except:
                self.logger.warning("No tweets found or page didn't load properly")

            # Seed from DOM immediately so small -n runs finish fast even if GraphQL is stale.
            await self._scrape_tweets_from_dom()
            if self.max_tweets_per_session and len(self.all_tweets) >= self.max_tweets_per_session:
                self.logger.info(f"Reached limit from initial DOM scrape: {len(self.all_tweets)}")
            else:
                await self._scroll_timeline(resume_from_tweet_id=resume_from_tweet_id, existing_tweet_ids=self.existing_tweet_ids)
            
            elapsed_time = time.time() - self.start_time
            self.logger.info(f"Scraping completed in {elapsed_time:.1f}s")
            self.logger.info(f"Total tweets collected: {len(self.all_tweets)}")
            
            return {
                'username': username,
                'user_data': self.user_data,
                'tweets': self.all_tweets,
                'tweet_count': len(self.all_tweets),
                'elapsed_time': elapsed_time
            }
            
        except Exception as e:
            self.logger.error(f"Error scraping user tweets: {e}")
            return {'error': str(e), 'tweets': self.all_tweets}
    
    def _prepare_scraping_session(self, username: Optional[str] = None, max_tweets: Optional[int] = None, 
                                  existing_tweet_ids: Optional[set] = None) -> None:
        if username:
            self.current_username = username
        self.start_time = time.time()
        self.scraped_tweet_ids.clear()
        self.all_tweets.clear()
        self.user_data = None
        self.max_tweets_per_session = max_tweets
        self.existing_tweet_ids = existing_tweet_ids or set()
    
    @retry_on_network_error(max_retries=3, delay=10.0, exceptions=(Exception,))
    async def _navigate_with_retry(self, url: str, max_retries: int = 3) -> bool:
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        self.logger.info(f"Navigating to URL...")
        
        try:
            await self.page.goto(url, 
                               wait_until='domcontentloaded',
                               timeout=self.timeouts['page_load_timeout'])
            await asyncio.sleep(self.timeouts['post_navigation_delay'])
            return True
        except Exception as e:
            self.logger.error(f"Failed to navigate to {url}")
            raise PageLoadError(f"Navigation failed: {e}") from e
    
    async def _wait_for_tweets(self, timeout: Optional[int] = None) -> bool:
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        timeout = timeout or self.timeouts['button_click_timeout']
        try:
            await self.page.wait_for_selector('[data-testid="tweet"]', timeout=timeout)
            self.logger.info("Search results loaded successfully")
            return True
        except Exception:
            self.logger.warning("No tweets found in search results")
            return False
    
    def _build_search_url(self, username: Optional[str] = None, since_date: Optional[str] = None, until_date: Optional[str] = None, query: Optional[str] = None, result_type: str = "live") -> str:
        
        if query:
            search_query = query
        elif username and since_date and until_date:
            search_query = f"from:{username} since:{since_date} until:{until_date}"
        elif username:
            search_query = f"from:{username}"
        else:
            raise ValueError("Must provide either query or username")
        
        encoded_query = urllib.parse.quote(search_query)
        return f"https://twitter.com/search?q={encoded_query}&src=typed_query&f={result_type}"
    
    async def scrape_user_tweets_by_search(self, username: str, since_date: str, until_date: str, 
                                           max_tweets_per_range: Optional[int] = None,
                                           existing_tweet_ids: Optional[set] = None) -> Dict[str, Any]:
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        try:
            self._prepare_scraping_session(username, max_tweets_per_range, existing_tweet_ids)
            
            self.logger.info(f"Starting SEARCH scrape for @{username}")
            self.logger.info(f"   Date range: {since_date} to {until_date}")
            limit_info = f" (limit: {max_tweets_per_range} tweets)" if max_tweets_per_range else " (unlimited)"
            self.logger.info(f"   {limit_info}")
            
            search_url = self._build_search_url(username=username, since_date=since_date, until_date=until_date)
            await self._navigate_with_retry(search_url)
            
            if not await self._wait_for_tweets():
                return {
                    'username': username,
                    'user_data': None,
                    'tweets': [],
                    'tweet_count': 0,
                    'elapsed_time': 0,
                    'date_range': {'since': since_date, 'until': until_date}
                }
            
            await self._scroll_timeline(resume_from_tweet_id=None, existing_tweet_ids=self.existing_tweet_ids)
            
            elapsed_time: float = time.time() - (self.start_time or 0)
            self.logger.info(f"Search scraping completed in {elapsed_time:.1f}s")
            self.logger.info(f"Total tweets collected from {since_date} to {until_date}: {len(self.all_tweets)}")
            
            return {
                'username': username,
                'user_data': self.user_data,
                'tweets': self.all_tweets,
                'tweet_count': len(self.all_tweets),
                'elapsed_time': elapsed_time,
                'date_range': {'since': since_date, 'until': until_date}
            }
            
        except Exception as e:
            self.logger.error(f"Error in search scraping: {e}")
            return {
                'error': str(e), 
                'tweets': self.all_tweets,
                'date_range': {'since': since_date, 'until': until_date}
            }
    
    async def search_tweets(self, query: str, max_tweets: Optional[int] = None, result_type: str = "Latest") -> Dict[str, Any]:
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        try:
            self._prepare_scraping_session(username=None, max_tweets=max_tweets, existing_tweet_ids=set())
            
            result_type_map = {
                "Latest": "live",
                "Top": "top",
                "Media": "image"
            }
            search_type = result_type_map.get(result_type, "live")
            
            self.logger.info(f"Starting KEYWORD search for: '{query}'")
            limit_info = f" (limit: {max_tweets} tweets)" if max_tweets else " (unlimited)"
            self.logger.info(f"   Result type: {result_type}{limit_info}")
            
            search_url = self._build_search_url(query=query, result_type=search_type)
            await self._navigate_with_retry(search_url)
            
            if not await self._wait_for_tweets():
                return {
                    'query': query,
                    'user_data': None,
                    'tweets': [],
                    'tweet_count': 0,
                    'elapsed_time': 0
                }
            
            await self._scroll_timeline(resume_from_tweet_id=None, existing_tweet_ids=set())
            
            elapsed_time: float = time.time() - (self.start_time or 0)
            self.logger.info(f"Search completed in {elapsed_time:.1f}s")
            self.logger.info(f"Total tweets collected for '{query}': {len(self.all_tweets)}")
            
            return {
                'query': query,
                'user_data': None,
                'tweets': self.all_tweets,
                'tweet_count': len(self.all_tweets),
                'elapsed_time': elapsed_time
            }
            
        except Exception as e:
            self.logger.error(f"Error in keyword search: {e}")
            return {
                'error': str(e), 
                'query': query,
                'tweets': self.all_tweets
            }

    async def _scrape_tweets_from_dom(self) -> int:
        """Fallback: pull visible tweets from the rendered timeline when GraphQL parse fails."""
        if not self.page:
            return 0

        added = 0
        try:
            articles = self.page.locator('article[data-testid="tweet"]')
            count = await articles.count()
            self.logger.info(f"DOM fallback found {count} tweet articles")

            for i in range(count):
                if self.max_tweets_per_session and len(self.all_tweets) >= self.max_tweets_per_session:
                    break
                post = articles.nth(i)
                try:
                    text = await post.locator('[data-testid="tweetText"]').inner_text(timeout=2000)
                except Exception:
                    text = ''
                try:
                    href = await post.locator('a[href*="/status/"]').first.get_attribute('href', timeout=2000)
                except Exception:
                    href = None
                if not href:
                    continue

                tweet_id = href.rstrip('/').split('/')[-1].split('?')[0]
                if not tweet_id or not tweet_id.isdigit():
                    continue
                if tweet_id in self.scraped_tweet_ids:
                    continue
                if hasattr(self, 'existing_tweet_ids') and tweet_id in self.existing_tweet_ids:
                    continue

                username = ''
                try:
                    # href like /FabrizioRomano/status/123
                    parts = href.split('/')
                    if len(parts) >= 2 and parts[1]:
                        username = parts[1]
                except Exception:
                    pass

                tweet = {
                    'id': tweet_id,
                    'text': text,
                    'created_at': '',
                    'url': f'https://x.com{href}' if href.startswith('/') else href,
                    'user': {
                        'username': username,
                        'display_name': username,
                    },
                    'metrics': {},
                    'scraped_at': time.time(),
                    'source': 'dom',
                }
                self.all_tweets.append(tweet)
                self.scraped_tweet_ids.add(tweet_id)
                added += 1

            if added:
                self.logger.info(f"DOM fallback extracted {added} tweets (total: {len(self.all_tweets)})")
        except Exception as e:
            self.logger.warning(f"DOM fallback failed: {e}")
        return added
    
    async def _scroll_timeline(self, resume_from_tweet_id: Optional[str] = None, existing_tweet_ids: Optional[set] = None):
        if not self.page:
            raise RuntimeError("Browser not initialized")
            
        limit = self.max_tweets_per_session or '?'
        self.logger.info(f"Starting timeline scroll (have {len(self.all_tweets)}/{limit})...")
        print(f"[scrape] scrolling… {len(self.all_tweets)}/{limit} tweets so far", flush=True)
        
        scroll_attempts = 0
        self.scroll_attempts_without_new = 0
        resume_point_found = False if resume_from_tweet_id else True
        existing_tweet_ids = existing_tweet_ids or set() 
        
        while scroll_attempts < self.max_scroll_attempts:
            scroll_attempts += 1
            tweets_before = len(self.all_tweets)

            try:
                url = self.page.url
                dom_count = await self.page.locator('article[data-testid="tweet"]').count()
            except Exception:
                url = '?'
                dom_count = -1
            
            
            await self.page.evaluate('window.scrollBy(0, window.innerHeight * 0.8)')
            
            
            delay = random.uniform(self.scroll_delay_min, self.scroll_delay_max)
            await asyncio.sleep(delay)

            # Always harvest visible DOM tweets; GraphQL intercept is often stale vs X UI.
            await self._scrape_tweets_from_dom()
            
            
            tweets_after = len(self.all_tweets)
            new_tweets = tweets_after - tweets_before

            msg = (
                f"[scrape] scroll {scroll_attempts}: +{new_tweets} "
                f"(total {tweets_after}/{limit}, dom_articles={dom_count}, url={url[:80]})"
            )
            print(msg, flush=True)
            self.logger.info(msg)
            
            
            if resume_from_tweet_id and not resume_point_found and existing_tweet_ids:
                overlap_count = sum(1 for tweet in self.all_tweets if tweet.get('id') in existing_tweet_ids)
                
                if overlap_count >= self.overlap_threshold:  
                    resume_point_found = True
                    self.logger.info(f"Found overlap zone! Detected {overlap_count} existing tweets")
                    self.logger.info(f"   Clearing {len(self.all_tweets)} tweets (duplicates + recent)...")
                    self.logger.info(f"   Now collecting OLDER tweets (before previous session)...")
                    
                    self.all_tweets.clear()
                    self.scraped_tweet_ids.clear()
                    
                    tweets_before = 0
                    tweets_after = 0
                    new_tweets = 0
                    self.scroll_attempts_without_new = 0
            
            if new_tweets > 0:
                self.scroll_attempts_without_new = 0
            else:
                self.scroll_attempts_without_new += 1
                
                if scroll_attempts >= 8 and len(self.all_tweets) == 0:
                    self.logger.error("Scrolled 8 times with 0 tweets extracted - stopping early")
                    print("[scrape] STOP: 0 tweets after 8 scrolls", flush=True)
                    break
                
                if not resume_point_found and self.scroll_attempts_without_new >= 100:
                    self.logger.warning(f"Scrolled 100 times without finding resume point - might not exist")
                    break
                elif resume_point_found and self.scroll_attempts_without_new >= self.max_attempts_without_new:
                    self.logger.info(f"No new tweets for {self.max_attempts_without_new} scrolls - stopping")
                    print(f"[scrape] STOP: no new tweets for {self.max_attempts_without_new} scrolls", flush=True)
                    break
            
            if resume_point_found and self.max_tweets_per_session and len(self.all_tweets) >= self.max_tweets_per_session:
                self.logger.info(f"Session limit reached: {len(self.all_tweets)}/{self.max_tweets_per_session} tweets")
                print(f"[scrape] DONE: reached limit {self.max_tweets_per_session}", flush=True)
                break
            
            
            is_at_bottom = await self.page.evaluate('''
                () => {
                    return window.innerHeight + window.scrollY >= document.body.scrollHeight - 100;
                }
            ''')
            

            if is_at_bottom and self.scroll_attempts_without_new > 10:
                self.logger.info("Reached bottom of timeline and no new tweets - stopping")
                print("[scrape] STOP: bottom of timeline", flush=True)
                break
            
            
            if scroll_attempts % 100 == 0:
                self.logger.info("Deep page refresh to trigger more tweet loading...")
                await self.page.evaluate('window.scrollTo(0, 0);')  
                await asyncio.sleep(self.timeouts['page_refresh_short_delay'])
                await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight);') 
                await asyncio.sleep(self.timeouts['page_refresh_long_delay'])
            
            
            if scroll_attempts % 50 == 0:
                elapsed = (time.time() - self.start_time) if self.start_time is not None else 0
                rate = len(self.all_tweets) / elapsed if elapsed > 0 else 0
                self.logger.info(f"Progress: {len(self.all_tweets)} tweets in {elapsed:.0f}s ({rate:.1f} tweets/s)")
                
            if self.start_time is not None:
                elapsed_time: float = time.time() - self.start_time
                if elapsed_time > 600 and len(self.all_tweets) == 0:
                    self.logger.error("Been scrolling for 10 minutes with 0 tweets - stopping to prevent crash")
                    self.logger.error("   This usually means tweet extraction is broken")
                    break
        
        self.logger.info(f"Scrolling completed after {scroll_attempts} attempts")
    
    def _save_final_tweets(self, username: str):    
        if not self.all_tweets:
            self.logger.warning("No tweets to save")
            return
        
        try:
            data_dir = Path(f"data/{username}")
            data_dir.mkdir(parents=True, exist_ok=True)
            
            
            filename = data_dir / f"tweets_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            
            output_data = {
                'username': username,
                'user_data': self.user_data,
                'tweet_count': len(self.all_tweets),
                'scraped_at': datetime.now().isoformat(),
                'scraping_duration_seconds': (time.time() - self.start_time) if self.start_time is not None else 0,
                'tweets': self.all_tweets
            }
            
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False, default=str)
            
            self.logger.info(f"Saved {len(self.all_tweets)} tweets to {filename}")
            
        except Exception as e:
            self.logger.error(f"Error saving tweets: {e}")
    
    async def cleanup(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.logger.info("Playwright resources cleaned up")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")