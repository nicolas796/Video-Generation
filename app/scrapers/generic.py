"""Generic e-commerce product scraper using JSON-LD and OpenGraph."""
import re
import json
import requests
from typing import Dict, List, Optional, Any, Set
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

class GenericScraper:
    """Generic scraper that works with any e-commerce site using structured data."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
    
    def scrape(self, url: str) -> Dict[str, Any]:
        """Scrape product data from any e-commerce URL."""
        try:
            response = self.session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            html_content = response.text
            soup = BeautifulSoup(html_content, 'html.parser')
            
            product_data = {
                'name': '',
                'description': '',
                'description_html': '',
                'brand': '',
                'price': '',
                'currency': '',
                'compare_at_price': '',
                'images': [],
                'variants': [],
                'specifications': {},
                'reviews': [],
                'url': response.url,
                'platform': 'generic',
                'raw_data': {}
            }
            
            # Try JSON-LD first (most reliable)
            jsonld_data = self._extract_jsonld(html_content)
            if jsonld_data:
                product_data['raw_data']['jsonld'] = jsonld_data
                self._parse_jsonld(jsonld_data, product_data)
            
            # Extract OpenGraph tags
            og_data = self._extract_opengraph(html_content)
            if og_data:
                product_data['raw_data']['opengraph'] = og_data
                self._apply_opengraph_data(og_data, product_data)
            
            # Extract Twitter Card data
            twitter_data = self._extract_twitter_cards(html_content)
            if twitter_data:
                product_data['raw_data']['twitter'] = twitter_data
                self._apply_twitter_data(twitter_data, product_data)
            
            # Extract meta tags as fallback
            meta_data = self._extract_meta_tags(html_content)
            if meta_data:
                product_data['raw_data']['meta'] = meta_data
                self._apply_meta_fallbacks(meta_data, product_data)
            
            # Try to find product data in page scripts (common patterns)
            script_data = self._extract_script_data(html_content)
            if script_data:
                product_data['raw_data']['scripts'] = script_data
                self._parse_script_data(script_data, product_data)

            if soup:
                description_candidate = extract_rich_description_from_soup(soup)
                if description_candidate:
                    merge_description(product_data, description_candidate)
            
            # Extract specifications
            self._extract_specifications(html_content, product_data)
            
            # Extract reviews
            self._extract_reviews(html_content, product_data)
            
            # Find additional images from page
            self._extract_page_images(html_content, product_data, response.url, soup=soup)
            
            # Clean up image URLs
            product_data['images'] = self._normalize_images(product_data['images'], response.url)
            
            # Try to detect platform
            product_data['platform'] = self._detect_platform(html_content, response.url)
            
            return product_data
            
        except requests.exceptions.RequestException as e:
            return {
                'error': f'Request failed: {str(e)}',
                'url': url,
                'platform': 'generic'
            }
        except Exception as e:
            return {
                'error': f'Scraping failed: {str(e)}',
                'url': url,
                'platform': 'generic'
            }
    
    def _extract_jsonld(self, html: str) -> Optional[Dict]:
        """Extract JSON-LD structured data from HTML."""
        patterns = [
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match.strip())
                    
                    # Handle @graph structure
                    if isinstance(data, dict) and '@graph' in data:
                        for item in data['@graph']:
                            if item.get('@type') in ['Product', 'IndividualProduct']:
                                return item
                    
                    # Handle direct Product
                    if isinstance(data, dict) and data.get('@type') in ['Product', 'IndividualProduct']:
                        return data
                    
                    # Handle array of items
                    if isinstance(data, list):
                        for item in data:
                            if item.get('@type') in ['Product', 'IndividualProduct']:
                                return item
                                
                except json.JSONDecodeError:
                    continue
        return None
    
    def _extract_opengraph(self, html: str) -> Dict[str, str]:
        """Extract OpenGraph meta tags."""
        og_data = {}
        
        patterns = {
            'title': r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
            'description': r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
            'image': r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            'image_secure': r'<meta[^>]*property=["\']og:image:secure_url["\'][^>]*content=["\']([^"\']+)["\']',
            'type': r'<meta[^>]*property=["\']og:type["\'][^>]*content=["\']([^"\']+)["\']',
            'url': r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']',
            'site_name': r'<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']',
            'price': r'<meta[^>]*property=["\']og:price:amount["\'][^>]*content=["\']([^"\']+)["\']',
            'currency': r'<meta[^>]*property=["\']og:price:currency["\'][^>]*content=["\']([^"\']+)["\']',
            'availability': r'<meta[^>]*property=["\']og:availability["\'][^>]*content=["\']([^"\']+)["\']',
            'brand': r'<meta[^>]*property=["\']og:brand["\'][^>]*content=["\']([^"\']+)["\']',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                og_data[key] = match.group(1)
        
        # Also look for product:original_price:amount
        match = re.search(r'<meta[^>]*property=["\']product:original_price:amount["\'][^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if match:
            og_data['original_price'] = match.group(1)
        
        return og_data
    
    def _extract_twitter_cards(self, html: str) -> Dict[str, str]:
        """Extract Twitter Card meta tags."""
        twitter_data = {}
        
        patterns = {
            'title': r'<meta[^>]*name=["\']twitter:title["\'][^>]*content=["\']([^"\']+)["\']',
            'description': r'<meta[^>]*name=["\']twitter:description["\'][^>]*content=["\']([^"\']+)["\']',
            'image': r'<meta[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
            'card': r'<meta[^>]*name=["\']twitter:card["\'][^>]*content=["\']([^"\']+)["\']',
            'site': r'<meta[^>]*name=["\']twitter:site["\'][^>]*content=["\']([^"\']+)["\']',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                twitter_data[key] = match.group(1)
        
        return twitter_data
    
    def _extract_meta_tags(self, html: str) -> Dict[str, str]:
        """Extract standard meta tags."""
        meta_data = {}
        
        patterns = {
            'title': r'<title>([^<]+)</title>',
            'description': r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']+)["\']',
            'keywords': r'<meta[^>]*name=["\']keywords["\'][^>]*content=["\']([^"\']+)["\']',
            'author': r'<meta[^>]*name=["\']author["\'][^>]*content=["\']([^"\']+)["\']',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                meta_data[key] = match.group(1).strip()
        
        return meta_data
    
    def _extract_script_data(self, html: str) -> Optional[Dict]:
        """Extract product data from JavaScript variables."""
        script_data = {}
        
        # Common patterns for product data in scripts
        patterns = [
            r'window\.__PRODUCT__\s*=\s*({.*?});',
            r'window\.__productData__\s*=\s*({.*?});',
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'var\s+productData\s*=\s*({[\s\S]*?});',
            r'"product":\s*({[\s\S]*?"id"[\s\S]*?})',
            r'data-product\s*=\s*[\'"]({.*?})[\'"]',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match.strip())
                    if isinstance(data, dict) and ('id' in data or 'title' in data or 'name' in data):
                        script_data['product'] = data
                        return script_data
                except json.JSONDecodeError:
                    continue
        
        return script_data if script_data else None
    
    def _parse_jsonld(self, data: Dict, product_data: Dict):
        """Parse JSON-LD data into product_data."""
        product_data['name'] = data.get('name', '')
        product_data['description'] = self._clean_description(data.get('description', ''))
        product_data['description_html'] = data.get('description', '')
        
        # Handle brand (can be string or dict)
        brand = data.get('brand', '')
        if isinstance(brand, dict):
            product_data['brand'] = brand.get('name', '')
        else:
            product_data['brand'] = brand
        
        # Parse offers
        offers = data.get('offers', {})
        if isinstance(offers, list) and offers:
            offers = offers[0]
        
        if isinstance(offers, dict):
            # Check for price in multiple locations
            price = offers.get('price', '')
            if not price:
                price_spec = offers.get('priceSpecification', {})
                if isinstance(price_spec, dict):
                    price = price_spec.get('price', '')
            product_data['price'] = str(price) if price else ''
            product_data['currency'] = offers.get('priceCurrency', '')
            
            # Check for availability
            availability = offers.get('availability', '')
            if availability:
                product_data['specifications']['Availability'] = availability.split('/')[-1]
            
            # Check for item condition
            condition = offers.get('itemCondition', '')
            if condition:
                product_data['specifications']['Condition'] = condition.split('/')[-1]
        
        # Parse images
        image_data = data.get('image', [])
        if isinstance(image_data, str):
            product_data['images'].append(image_data)
        elif isinstance(image_data, list):
            for img in image_data:
                if isinstance(img, str):
                    product_data['images'].append(img)
                elif isinstance(img, dict) and img.get('url'):
                    product_data['images'].append(img['url'])
        elif isinstance(image_data, dict):
            if 'url' in image_data:
                product_data['images'].append(image_data['url'])
        
        # Parse SKU, GTIN, MPN
        if data.get('sku'):
            product_data['specifications']['SKU'] = data.get('sku')
        if data.get('gtin'):
            product_data['specifications']['GTIN'] = data.get('gtin')
        if data.get('mpn'):
            product_data['specifications']['MPN'] = data.get('mpn')
        if data.get('productID'):
            product_data['specifications']['Product ID'] = data.get('productID')
        
        # Parse aggregate rating
        aggregate_rating = data.get('aggregateRating', {})
        if aggregate_rating:
            product_data['specifications']['Average Rating'] = str(aggregate_rating.get('ratingValue', ''))
            product_data['specifications']['Review Count'] = str(aggregate_rating.get('reviewCount', ''))
        
        # Parse reviews
        reviews = data.get('review', [])
        if isinstance(reviews, dict):
            reviews = [reviews]
        if isinstance(reviews, list):
            for review in reviews[:10]:  # Limit to 10 reviews
                if isinstance(review, dict):
                    product_data['reviews'].append({
                        'author': review.get('author', {}).get('name', 'Anonymous') if isinstance(review.get('author'), dict) else review.get('author', 'Anonymous'),
                        'rating': review.get('reviewRating', {}).get('ratingValue', 0) if isinstance(review.get('reviewRating'), dict) else review.get('rating', 0),
                        'title': review.get('name', ''),
                        'body': review.get('reviewBody', ''),
                        'date': review.get('datePublished', '')
                    })
    
    def _apply_opengraph_data(self, og_data: Dict, product_data: Dict):
        """Apply OpenGraph data."""
        if not product_data['name'] and og_data.get('title'):
            product_data['name'] = og_data['title']
        
        if not product_data['description'] and og_data.get('description'):
            product_data['description'] = self._clean_description(og_data['description'])
            product_data['description_html'] = f"<p>{og_data['description']}</p>"
        
        # Add images
        if og_data.get('image_secure'):
            product_data['images'].append(og_data['image_secure'])
        elif og_data.get('image'):
            product_data['images'].append(og_data['image'])
        
        if not product_data['price'] and og_data.get('price'):
            product_data['price'] = og_data['price']
        
        if not product_data['currency'] and og_data.get('currency'):
            product_data['currency'] = og_data['currency']
        
        if og_data.get('original_price'):
            product_data['compare_at_price'] = og_data['original_price']
        
        if og_data.get('availability'):
            product_data['specifications']['Availability'] = og_data['availability']
        
        if og_data.get('brand'):
            product_data['brand'] = og_data['brand']
        elif og_data.get('site_name') and not product_data['brand']:
            product_data['brand'] = og_data['site_name']
    
    def _apply_twitter_data(self, twitter_data: Dict, product_data: Dict):
        """Apply Twitter Card data as fallbacks."""
        if not product_data['name'] and twitter_data.get('title'):
            product_data['name'] = twitter_data['title']
        
        if not product_data['description'] and twitter_data.get('description'):
            product_data['description'] = self._clean_description(twitter_data['description'])
            product_data['description_html'] = f"<p>{twitter_data['description']}</p>"
        
        if twitter_data.get('image') and twitter_data['image'] not in product_data['images']:
            product_data['images'].append(twitter_data['image'])
    
    def _apply_meta_fallbacks(self, meta_data: Dict, product_data: Dict):
        """Apply meta tag data as fallbacks."""
        if not product_data['name']:
            if meta_data.get('title'):
                product_data['name'] = meta_data['title']
        
        if not product_data['description'] and meta_data.get('description'):
            product_data['description'] = self._clean_description(meta_data['description'])
            product_data['description_html'] = f"<p>{meta_data['description']}</p>"
        
        if meta_data.get('keywords'):
            product_data['specifications']['Keywords'] = meta_data['keywords']
    
    def _parse_script_data(self, script_data: Dict, product_data: Dict):
        """Parse script-based product data."""
        product = script_data.get('product', {})
        
        if not product_data['name'] and product.get('title'):
            product_data['name'] = product.get('title')
        if not product_data['name'] and product.get('name'):
            product_data['name'] = product.get('name')
        
        if not product_data['description'] and product.get('description'):
            product_data['description'] = self._clean_description(product.get('description'))
            product_data['description_html'] = product.get('description')
        
        if not product_data['price']:
            if product.get('price'):
                product_data['price'] = str(product.get('price'))
            elif product.get('priceInfo', {}).get('price'):
                product_data['price'] = str(product.get('priceInfo', {}).get('price'))
        
        if not product_data['compare_at_price'] and product.get('compareAtPrice'):
            product_data['compare_at_price'] = str(product.get('compareAtPrice'))
        
        # Parse images from script data
        images = product.get('images', [])
        if images:
            for img in images:
                if isinstance(img, str):
                    product_data['images'].append(img)
                elif isinstance(img, dict):
                    for key in ['url', 'src', 'large', 'original', 'full']:
                        if img.get(key):
                            product_data['images'].append(img[key])
                            break
    
    def _extract_specifications(self, html: str, product_data: Dict):
        """Extract product specifications from HTML."""
        # Look for common specification patterns
        spec_selectors = [
            # Table-based specs
            (r'<table[^>]*class=["\'][^"\']*spec[^"\']*["\'][^>]*>(.*?)</table>', 'table'),
            (r'<table[^>]*id=["\']specifications["\'][^>]*>(.*?)</table>', 'table'),
            # Div-based specs
            (r'<div[^>]*class=["\'][^"\']*product-spec[^"\']*["\'][^>]*>(.*?)</div>', 'div'),
            (r'<div[^>]*id=["\']product-details["\'][^>]*>(.*?)</div>', 'div'),
            # DL-based specs
            (r'<dl[^>]*class=["\'][^"\']*spec[^"\']*["\'][^>]*>(.*?)</dl>', 'dl'),
        ]
        
        for pattern, pattern_type in spec_selectors:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                content = match.group(1)
                
                if pattern_type == 'table':
                    # Parse table rows
                    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', content, re.DOTALL | re.IGNORECASE)
                    for row in rows:
                        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL | re.IGNORECASE)
                        if len(cells) >= 2:
                            label = self._strip_html(cells[0]).strip()
                            value = self._strip_html(cells[1]).strip()
                            if label and value and len(label) < 100:
                                product_data['specifications'][label] = value
                
                elif pattern_type == 'dl':
                    # Parse definition list
                    dts = re.findall(r'<dt[^>]*>(.*?)</dt>', content, re.DOTALL | re.IGNORECASE)
                    dds = re.findall(r'<dd[^>]*>(.*?)</dd>', content, re.DOTALL | re.IGNORECASE)
                    for dt, dd in zip(dts, dds):
                        label = self._strip_html(dt).strip()
                        value = self._strip_html(dd).strip()
                        if label and value:
                            product_data['specifications'][label] = value
                
                elif pattern_type == 'div':
                    # Try to find label-value pairs
                    pairs = re.findall(r'<div[^>]*class=["\'][^"\']*label["\'][^>]*>(.*?)</div>\s*<div[^>]*class=["\'][^"\']*value["\'][^>]*>(.*?)</div>', content, re.DOTALL | re.IGNORECASE)
                    for label, value in pairs:
                        label = self._strip_html(label).strip()
                        value = self._strip_html(value).strip()
                        if label and value:
                            product_data['specifications'][label] = value
                
                break  # Stop after first successful extraction
    
    def _extract_reviews(self, html: str, product_data: Dict):
        """Extract reviews from various formats."""
        # Try to find review data in scripts
        review_patterns = [
            r'"reviews":\s*(\[[^\]]*\])',
            r'window\.__REVIEWS__\s*=\s*({.*?});',
            r'"aggregateRating":\s*({[^}]*})',
        ]
        
        for pattern in review_patterns:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match.strip())
                    if isinstance(data, dict) and 'reviewCount' in data:
                        product_data['specifications']['Review Count'] = str(data.get('reviewCount', ''))
                        product_data['specifications']['Average Rating'] = str(data.get('ratingValue', ''))
                    break
                except:
                    pass
    
    def _extract_page_images(self, html: str, product_data: Dict, base_url: str, soup: Optional[BeautifulSoup] = None):
        """Extract additional product images from page."""
        if soup is None:
            soup = BeautifulSoup(html, 'html.parser')

        if soup:
            gallery_images = extract_gallery_images_from_soup(soup, base_url)
            for img_url in gallery_images:
                if img_url not in product_data['images']:
                    product_data['images'].append(img_url)
                    if len(product_data['images']) >= 12:
                        return

        # Fallback to regex-based extraction for non-standard layouts
        if len(product_data['images']) >= 5:
            return

        image_patterns = [
            r'<img[^>]*class=["\'][^"\']*product-image[^"\']*["\'][^>]*src=["\']([^"\']+)["\']',
            r'<img[^>]*data-zoom-image=["\']([^"\']+)["\']',
            r'<img[^>]*data-large-image=["\']([^"\']+)["\']',
            r'<a[^>]*data-image=["\']([^"\']+)["\'][^>]*>',
            r'"imageUrl":[\s]*["\']([^"\']+)["\']',
        ]

        skip_keywords = ['thumbnail', '_thumb', 'placeholder', 'loading', 'icon', 'logo']

        for pattern in image_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for img_url in matches:
                if any(skip in img_url.lower() for skip in skip_keywords):
                    continue
                if re.search(r'_?\d{1,2}x\d{1,2}[._]', img_url):
                    continue
                if img_url not in product_data['images']:
                    product_data['images'].append(img_url)
                    if len(product_data['images']) >= 12:
                        return

    def _detect_platform(self, html: str, url: str) -> str:
        """Try to detect the e-commerce platform."""
        url_lower = url.lower()
        html_lower = html.lower()
        
        # Check URL patterns
        if 'myshopify.com' in url_lower or 'cdn.shopify.com' in html_lower:
            return 'shopify'
        if 'woocommerce' in html_lower or 'wc-' in html_lower:
            return 'woocommerce'
        if 'bigcommerce' in html_lower or 'cdn11.bigcommerce.com' in html_lower:
            return 'bigcommerce'
        if 'magento' in html_lower:
            return 'magento'
        if 'squarespace' in html_lower:
            return 'squarespace'
        if 'wix' in html_lower:
            return 'wix'
        
        # Check for platform-specific scripts/css
        if 'woocommerce' in html_lower:
            return 'woocommerce'
        if 'wp-content' in html_lower:
            return 'wordpress'
        if 'prestashop' in html_lower:
            return 'prestashop'
        
        return 'generic'
    
    def _normalize_images(self, images: List[str], base_url: str) -> List[str]:
        """Normalize and deduplicate image URLs."""
        seen = set()
        normalized = []
        
        for img in images:
            if not img:
                continue

            img = upgrade_shopify_image_url(img)
            
            # Convert to absolute URL
            if img.startswith('//'):
                img = 'https:' + img
            elif img.startswith('/'):
                parsed_base = urlparse(base_url)
                img = f"{parsed_base.scheme}://{parsed_base.netloc}{img}"
            elif not img.startswith('http'):
                img = urljoin(base_url, img)
            
            # Remove query parameters and fragments for deduplication
            clean_url = img.split('?')[0].split('#')[0]
            
            if clean_url not in seen:
                seen.add(clean_url)
                normalized.append(img)
        
        return normalized
    
    def _strip_html(self, text: str) -> str:
        """Remove HTML tags from text."""
        if not text:
            return ''
        text = re.sub(r'<[^>]+>', '', text)
        import html as html_module
        text = html_module.unescape(text)
        return text.strip()
    
    def _clean_description(self, description: str) -> str:
        """Clean HTML description to plain text."""
        return clean_html_to_text(description)

def clean_html_to_text(description: str) -> str:
    """Convert HTML fragments into plain text while preserving line breaks and bullets."""
    if not description:
        return ''

    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', description, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<br\s*/?>', '\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<p[^>]*>', '\n\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</p>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<li[^>]*>', '\n• ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</li>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<h[1-6][^>]*>', '\n\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</h[1-6]>', '\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)

    import html as html_module
    cleaned = html_module.unescape(cleaned)
    lines = [line.strip() for line in cleaned.split('\n')]
    cleaned = '\n'.join(line for line in lines if line)
    return cleaned.strip()


def build_soup(html: str) -> Optional[BeautifulSoup]:
    """Create a BeautifulSoup parser with basic error handling."""
    if not html:
        return None
    try:
        return BeautifulSoup(html, 'html.parser')
    except Exception:
        return None


_DEF_SECTION_KEYWORDS = ('product', 'description', 'detail', 'feature', 'benefit', 'accordion', 'tab', 'info', 'content', 'copy', 'highlight')


def _is_product_section(element) -> bool:
    """Heuristic to determine if an element belongs to a product detail section."""
    try:
        current = element
        depth = 0
        while current and current.name not in ('body', 'html') and depth < 5:
            attributes = ' '.join(filter(None, [current.get('id', ''), ' '.join(current.get('class', []))])).lower()
            if any(keyword in attributes for keyword in _DEF_SECTION_KEYWORDS):
                return True
            current = current.parent
            depth += 1
    except AttributeError:
        return False
    return False


def extract_rich_description_from_soup(soup: BeautifulSoup) -> Optional[Dict[str, str]]:
    """Extract the most complete description block (with bullets if possible)."""
    if not soup:
        return None

    selectors = [
        '.product-description',
        '.product__description',
        '.description',
        '[data-product-description]',
        '.product-details__description',
        '#ProductDescription',
        '.product-copy',
        '.product-info',
        '.rte',
        '.accordion__body',
    ]

    candidates = []
    seen = set()

    def add_candidate(node, boost=0):
        if not node:
            return
        html_fragment = str(node)
        text_value = clean_html_to_text(html_fragment)
        normalized = text_value.strip()
        if len(normalized) < 60 or len(normalized) > 5000:
            return
        if normalized in seen:
            return
        has_bullets = bool(node.find_all('li')) or bool(re.search(r'[✓•●]', normalized)) or bool(re.search(r'(?:^|\n)\s*[-–•]', normalized))
        score = len(normalized) + boost + (200 if has_bullets else 0)
        candidates.append({'text': normalized, 'html': html_fragment, 'score': score, 'has_bullets': has_bullets})
        seen.add(normalized)

    for selector in selectors:
        for match in soup.select(selector):
            add_candidate(match, boost=100)

    for ul in soup.find_all('ul'):
        if len(ul.find_all('li')) < 2:
            continue
        if _is_product_section(ul):
            add_candidate(ul, boost=120)

    bullet_pattern = re.compile(r'(?:^|\n)\s*(?:[✓•●]|[-–])')
    for tag in soup.find_all(['p', 'div', 'span']):
        text_value = tag.get_text(separator=' ', strip=True)
        if not text_value or len(text_value) < 30:
            continue
        if not bullet_pattern.search(text_value):
            continue
        parent = tag
        steps = 0
        while parent.parent and parent.parent.name not in ('body', 'html') and steps < 2:
            parent = parent.parent
            steps += 1
        if _is_product_section(parent):
            add_candidate(parent, boost=80)

    if not candidates:
        return None

    candidates.sort(key=lambda item: item['score'], reverse=True)
    best = candidates[0]

    if not best['has_bullets']:
        bullet_candidate = next((item for item in candidates if item['has_bullets']), None)
        if bullet_candidate and bullet_candidate['text'] not in best['text']:
            return {
                'text': f"{best['text']}\n\n{bullet_candidate['text']}".strip(),
                'html': f"{best['html']}\n{bullet_candidate['html']}"
            }

    return {'text': best['text'], 'html': best['html']}


def merge_description(product_data: Dict[str, Any], candidate: Dict[str, str]) -> None:
    """Merge a new description candidate into the product payload."""
    if not candidate:
        return

    text_value = candidate.get('text', '').strip()
    html_value = candidate.get('html', '').strip()
    if not text_value:
        return

    existing = product_data.get('description', '').strip()
    existing_html = product_data.get('description_html', '').strip()

    if not existing:
        product_data['description'] = text_value
        product_data['description_html'] = html_value
        return

    needs_bullets = any(symbol in text_value for symbol in ('✓', '•', '●', '- ')) and not any(symbol in existing for symbol in ('✓', '•', '●', '- '))
    replace_with_candidate = len(text_value) > len(existing) * 1.2

    if replace_with_candidate:
        product_data['description'] = text_value
        product_data['description_html'] = html_value or candidate.get('html', '')
        return

    if needs_bullets and text_value not in existing:
        combined_text = f"{existing}\n\n{text_value}".strip()
        combined_html = f"{existing_html}\n{html_value}".strip() if existing_html and html_value else (html_value or existing_html)
        product_data['description'] = combined_text
        product_data['description_html'] = combined_html


def extract_gallery_images_from_soup(soup: BeautifulSoup, base_url: str) -> List[str]:
    """Collect high-quality gallery images from common Shopify DOM patterns."""
    if not soup:
        return []

    selectors = [
        '.product-gallery img',
        '.product__media img',
        '.product__slides img',
        '.product-images img',
        '[data-product-images] img',
        '.media-gallery img',
        '.gallery__image img',
        '.product-media img',
        '.featured-product img',
    ]

    handle = _infer_product_handle(base_url)
    collected: List[str] = []
    seen: Set[str] = set()

    def consider_image(url: str, force: bool = False):
        if not url or url.startswith('data:'):
            return
        upgraded = upgrade_shopify_image_url(url.strip())
        key = upgraded.split('?')[0]
        if key in seen:
            return
        if force or _looks_like_product_image(upgraded, handle):
            seen.add(key)
            collected.append(upgraded)

    for selector in selectors:
        for img in soup.select(selector):
            for src in _extract_image_sources_from_tag(img):
                consider_image(src, force=True)

    lazy_attrs = ['data-src', 'data-original', 'data-image', 'data-zoom-image', 'data-large-image', 'data-srcset', 'data-lazy-src', 'data-flickity-lazyload']
    for img in soup.find_all('img'):
        if not any(attr in img.attrs for attr in lazy_attrs) and not img.get('src'):
            continue
        for src in _extract_image_sources_from_tag(img):
            consider_image(src)

    return collected


def _extract_image_sources_from_tag(tag) -> List[str]:
    sources: List[str] = []
    attr_order = [
        'data-zoom-image',
        'data-original',
        'data-image',
        'data-large-image',
        'data-src',
        'data-lazy-src',
        'data-flickity-lazyload',
        'data-srcset',
        'srcset',
        'src',
    ]

    for attr in attr_order:
        value = tag.get(attr)
        if not value:
            continue
        if 'srcset' in attr:
            entries = [entry.strip() for entry in value.split(',') if entry.strip()]
            if entries:
                sources.append(entries[-1].split(' ')[0])
        else:
            sources.append(value)

    style_attr = tag.get('style', '')
    match = re.search(r'url\(([^)]+)\)', style_attr)
    if match:
        candidate = match.group(1).strip().strip('"\'')
        sources.append(candidate)

    return sources


def _looks_like_product_image(url: str, handle: Optional[str]) -> bool:
    lowered = url.lower()
    skip_keywords = ('sprite', 'icon', 'logo', 'badge', 'placeholder', 'thumb', 'thumbnail', 'avatar', 'loading', 'spinner', 'gif')
    if any(keyword in lowered for keyword in skip_keywords):
        return False
    if re.search(r'_?\d{1,2}x\d{1,2}[._]', lowered):
        return False
    if handle and handle in lowered:
        return True
    if 'cdn.shopify.com' in lowered or '/products/' in lowered:
        return True
    return False


def _infer_product_handle(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    segments = [segment for segment in parsed.path.split('/') if segment]
    if 'products' in segments:
        idx = segments.index('products')
        if idx + 1 < len(segments):
            return segments[idx + 1]
    if segments:
        return segments[-1]
    return None


def upgrade_shopify_image_url(url: str) -> str:
    if not url or 'cdn.shopify.com' not in url:
        return url
    pattern = re.compile(r'_(?:pico|icon|thumb|small|compact|medium|large|grande|[0-9]+x[0-9]+|[0-9]+x)(?:@[0-9]+x)?(?=[_.])')
    return pattern.sub('_large', url)



# Convenience function
def scrape_generic(url: str) -> Dict[str, Any]:
    """Scrape a generic e-commerce product URL."""
    scraper = GenericScraper()
    return scraper.scrape(url)
