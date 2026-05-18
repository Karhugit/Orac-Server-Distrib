# -*- coding: utf-8 -*-
import re
from urllib.parse import quote_plus, urljoin
from resources.scrapers.modules import client
from resources.lib.log_utils import log, LOGERROR, LOGINFO, LOGDEBUG, LOGWARNING

class MetacriticScraper:
    def __init__(self):
        self.base_url = "https://www.metacritic.com"
        self.search_url = "https://www.metacritic.com/search/{query}/" # New format confirmed by subagent
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.metacritic.com/',
        }

    def get_slug(self, title, year=None):
        try:
            query = quote_plus(title.lower())
            search_url = self.search_url.format(query=query)
            log(f"[Metacritic] Searching: {search_url}", level=LOGDEBUG)
            
            html = client.request(search_url, headers=self.headers)
            if not html:
                log(f"[Metacritic] Search failed (no HTML) for: {title}", level=LOGERROR)
                return None

            # Flexible regex for search items
            # <a class="c-search-item search-item__content" href="/movie/the-matrix/">
            item_regex = r'<a[^>]*class="[^"]*c-search-item[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
            matches = re.findall(item_regex, html, re.S)
            
            if not matches:
                # Try fallback if order is different
                item_regex = r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*c-search-item[^"]*"[^>]*>(.*?)</a>'
                matches = re.findall(item_regex, html, re.S)

            if not matches:
                log(f"[Metacritic] No search results for: {title}", level=LOGDEBUG)
                return None

            best_match = None
            title_clean = re.sub(r'[^a-zA-Z0-9]', '', title.lower())

            for slug, content in matches:
                if "/movie/" not in slug: continue
                
                # Check for title match in content
                # Search item structure: <p>The Matrix</p>
                p_match = re.search(r'<p>(.*?)</p>', content, re.S)
                res_title = p_match.group(1).strip() if p_match else ""
                res_title_clean = re.sub(r'[^a-zA-Z0-9]', '', res_title.lower())
                
                if title_clean == res_title_clean:
                    if not year or str(year) in content:
                        log(f"[Metacritic] Match found: {slug}", level=LOGDEBUG)
                        return slug
                    if not best_match:
                        best_match = slug
            
            return best_match or matches[0][0]

        except Exception as e:
            log(f"[Metacritic] Error in get_slug: {e}", level=LOGERROR)
            return None

    def get_reviews(self, title, year=None, max_critic=15, max_user=15):
        slug = self.get_slug(title, year)
        if not slug:
            return []

        reviews = []
        critic_url = urljoin(self.base_url, slug.strip('/') + '/critic-reviews/')
        reviews.extend(self.scrape_reviews(critic_url, 'critic', max_critic))
        
        user_url = urljoin(self.base_url, slug.strip('/') + '/user-reviews/')
        reviews.extend(self.scrape_reviews(user_url, 'user', max_user))
        
        return reviews

    def scrape_reviews(self, url, review_type, max_items):
        try:
            log(f"[Metacritic] Scraping {review_type}: {url}", level=LOGDEBUG)
            html = client.request(url, headers=self.headers)
            if not html:
                return []

            from resources.scrapers.modules.client import replaceHTMLCodes
            
            # Review card pattern based on raw HTML capture
            # <div class="review-card" ...>...</div>
            # We use a non-greedy catch for the content.
            # However, cards can contain nested divs, so regex might be tricky.
            # But we can find the start of each card.
            
            card_starts = [m.start() for m in re.finditer(r'<div[^>]*class="review-card"', html)]
            print(f"[Metacritic] Found {len(card_starts)} review card starts")

            results = []
            for i in range(len(card_starts)):
                if len(results) >= max_items:
                    break
                
                start = card_starts[i]
                end = card_starts[i+1] if i+1 < len(card_starts) else len(html)
                block = html[start:end]
                
                # Score: <div class="...c-siteReviewScore" ...><span ...>100</span></div>
                score_match = re.search(r'class="[^"]*c-siteReviewScore[^"]*"[^>]*>.*?<span[^>]*>(\d+)</span>', block, re.S)
                score = score_match.group(1) if score_match else "0"
                
                # Author/Publication: <a class="review-card__header" ...>...</a>
                # The content inside <a> has the score div followed by the name.
                header_match = re.search(r'<a[^>]*class="review-card__header"[^>]*>(.*?)</a>', block, re.S)
                if header_match:
                    header_content = header_match.group(1)
                    # Remove all HTML from header content to get the name
                    author = re.sub(r'<[^>]+>', '', header_content).strip()
                    # Remove the score digit if it's there
                    author = author.replace(score, "").strip()
                else:
                    author = "Unknown"
                
                author = replaceHTMLCodes(author)

                # Snippet: <div class="review-card__quote" ...>...</div>
                snippet_match = re.search(r'class="review-card__quote"[^>]*>(.*?)</div>', block, re.S)
                if snippet_match:
                    snippet = re.sub(r'<[^>]+>', '', snippet_match.group(1)).strip()
                    snippet = replaceHTMLCodes(snippet)
                    
                    if snippet:
                        label = "[Metacritic Critic]" if review_type == 'critic' else "[Metacritic User]"
                        scale = "100" if review_type == 'critic' else "10"
                        text = '[B]%s %s[/B]  [B]Rating: %s/%s[/B][CR][CR]%s' % (label, author, score, scale, snippet)
                        results.append(text)
            
            return results

        except Exception as e:
            log(f"[Metacritic] Error in scrape_reviews: {e}", level=LOGERROR)
            return []
