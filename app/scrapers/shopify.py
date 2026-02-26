"""Shopify product scraper."""
import re
import json
import requests
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin, urlparse

from .generic import (
    build_soup,
    clean_html_to_text,
    extract_gallery_images_from_soup,
    extract_rich_description_from_soup,
    merge_description,
    upgrade_shopify_image_url,
)

class ShopifyScraper:
    """Scraper optimized for Shopify stores."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
    
    def is_shopify_url(self, url: str) -> bool:
        """Check if URL is likely a Shopify store."""
        try:
            response = self.session.head(url, timeout=10, allow_redirects=True)
            final_url = response.url
            
            # Check for Shopify indicators
            shopify_indicators = [
                'myshopify.com',
                'cdn.shopify.com',
                'shopifycdn.net',
                'shopify.com',
            ]
            
            for indicator in shopify_indicators:
                if indicator in final_url:
                    return True
            
            # Check response headers for Shopify
            powered_by = response.headers.get('X-Powered-By', '').lower()
            if 'shopify' in powered_by:
                return True
                
            return False
        except:
            return False
    
    def scrape(self, url: str) -> Dict[str, Any]:
        """Scrape product data from a Shopify URL."""
        try:
            response = self.session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            html_content = response.text
            soup = build_soup(html_content)
            
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
                'platform': 'shopify',
                'raw_data': {}
            }
            
            # Extract from JSON-LD structured data (Shopify's standard)
            jsonld_data = self._extract_jsonld(html_content)
            if jsonld_data:
                product_data['raw_data']['jsonld'] = jsonld_data
                self._parse_jsonld(jsonld_data, product_data)
            
            # Extract from Shopify's JavaScript data
            shopify_data = self._extract_shopify_data(html_content)
            if shopify_data:
                product_data['raw_data']['shopify'] = shopify_data
                self._parse_shopify_data(shopify_data, product_data)
            
            # Extract from meta tags (fallback)
            meta_data = self._extract_meta_tags(html_content)
            if meta_data:
                product_data['raw_data']['meta'] = meta_data
                self._apply_meta_fallbacks(meta_data, product_data)

            if soup:
                description_candidate = extract_rich_description_from_soup(soup)
                if description_candidate:
                    merge_description(product_data, description_candidate)
                gallery_images = extract_gallery_images_from_soup(soup, response.url)
                for img in gallery_images:
                    if img not in product_data['images']:
                        product_data['images'].append(img)
            
            # Extract specifications from description or dedicated sections
            self._extract_specifications(html_content, product_data)
            
            # Extract reviews if available
            self._extract_reviews(html_content, product_data)
            
            # Clean up image URLs
            product_data['images'] = self._normalize_images(product_data['images'], response.url)
            
            return product_data
            
        except requests.exceptions.RequestException as e:
            return {
                'error': f'Request failed: {str(e)}',
                'url': url,
                'platform': 'shopify'
            }
        except Exception as e:
            return {
                'error': f'Scraping failed: {str(e)}',
                'url': url,
                'platform': 'shopify'
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
                    # Look for Product type
                    if isinstance(data, dict) and data.get('@type') == 'Product':
                        return data
                    # Sometimes it's a graph
                    if isinstance(data, dict) and '@graph' in data:
                        for item in data['@graph']:
                            if item.get('@type') == 'Product':
                                return item
                except json.JSONDecodeError:
                    continue
        return None
    
    def _extract_shopify_data(self, html: str) -> Optional[Dict]:
        """Extract Shopify's product JavaScript data."""
        patterns = [
            r'window\.ShopifyAnalytics\s*=\s*({.*?});',
            r'var\s+meta\s*=\s*({[\s\S]*?"product"[\s\S]*?});',
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        
        # Try to find product data in script tags
        script_pattern = r'<script[^>]*>(.*?var\s+product\s*=\s*\{.*?)</script>'
        match = re.search(script_pattern, html, re.DOTALL | re.IGNORECASE)
        if match:
            # Extract just the product object
            product_match = re.search(r'var\s+product\s*=\s*({[\s\S]*?});', match.group(1))
            if product_match:
                try:
                    return json.loads(product_match.group(1))
                except:
                    pass
        
        return None
    
    def _extract_meta_tags(self, html: str) -> Dict[str, str]:
        """Extract product data from meta tags."""
        meta_data = {}
        
        patterns = {
            'title': r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']',
            'description': r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']+)["\']',
            'image': r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            'price': r'<meta[^>]*property=["\']og:price:amount["\'][^>]*content=["\']([^"\']+)["\']',
            'currency': r'<meta[^>]*property=["\']og:price:currency["\'][^>]*content=["\']([^"\']+)["\']',
            'brand': r'<meta[^>]*property=["\']og:brand["\'][^>]*content=["\']([^"\']+)["\']',
        }
        
        for key, pattern in patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                meta_data[key] = match.group(1)
        
        return meta_data
    
    def _parse_jsonld(self, data: Dict, product_data: Dict):
        """Parse JSON-LD data into product_data."""
        product_data['name'] = data.get('name', '')
        product_data['description'] = self._clean_description(data.get('description', ''))
        product_data['description_html'] = data.get('description', '')
        product_data['brand'] = data.get('brand', {}).get('name', '') if isinstance(data.get('brand'), dict) else data.get('brand', '')
        
        # Parse offers
        offers = data.get('offers', {})
        if isinstance(offers, list) and offers:
            offers = offers[0]
        
        if isinstance(offers, dict):
            price_spec = offers.get('priceSpecification') if isinstance(offers.get('priceSpecification'), dict) else None
            price_value = offers.get('price')
            if (price_value is None or price_value == '') and price_spec:
                price_value = price_spec.get('price')
            if price_value not in (None, ''):
                product_data['price'] = str(price_value)
            currency_value = offers.get('priceCurrency') or (price_spec.get('priceCurrency') if price_spec else None) or (price_spec.get('currency') if price_spec else None)
            if currency_value:
                product_data['currency'] = currency_value
            availability = offers.get('availability', '')
            product_data['specifications']['availability'] = availability.split('/')[-1] if availability else ''
        
        # Parse images
        image_data = data.get('image', [])
        if isinstance(image_data, str):
            product_data['images'].append(upgrade_shopify_image_url(image_data))
        elif isinstance(image_data, list):
            for img in image_data:
                if isinstance(img, str):
                    product_data['images'].append(upgrade_shopify_image_url(img))
                elif isinstance(img, dict) and img.get('url'):
                    product_data['images'].append(upgrade_shopify_image_url(img['url']))
        elif isinstance(image_data, dict):
            if 'url' in image_data:
                product_data['images'].append(upgrade_shopify_image_url(image_data['url']))
        
        # Parse SKU
        if data.get('sku'):
            product_data['specifications']['SKU'] = data.get('sku')
        
        # Parse GTIN/MPN
        if data.get('gtin'):
            product_data['specifications']['GTIN'] = data.get('gtin')
        if data.get('mpn'):
            product_data['specifications']['MPN'] = data.get('mpn')
    
    def _parse_shopify_data(self, data: Dict, product_data: Dict):
        """Parse Shopify-specific JavaScript data."""
        # Try to get product from meta
        product = data.get('product', data)
        
        if not product_data['name'] and product.get('title'):
            product_data['name'] = product.get('title')
        
        if not product_data['description'] and product.get('description'):
            product_data['description'] = self._clean_description(product.get('description'))
            product_data['description_html'] = product.get('description')
        
        # Parse variants
        variants = product.get('variants', [])
        if variants:
            for variant in variants:
                variant_data = {
                    'id': variant.get('id'),
                    'title': variant.get('title'),
                    'price': str(variant.get('price', '')) if variant.get('price') else '',
                    'compare_at_price': str(variant.get('compare_at_price', '')) if variant.get('compare_at_price') else '',
                    'sku': variant.get('sku'),
                    'available': variant.get('available', False),
                    'option_values': []
                }
                
                # Add option values
                for i in range(1, 4):
                    option_key = f'option{i}'
                    if variant.get(option_key):
                        variant_data['option_values'].append(variant.get(option_key))
                
                product_data['variants'].append(variant_data)
            
            # Use first variant price if no price set
            if not product_data['price'] and variants[0].get('price'):
                product_data['price'] = str(variants[0].get('price'))
            if not product_data['compare_at_price'] and variants[0].get('compare_at_price'):
                product_data['compare_at_price'] = str(variants[0].get('compare_at_price'))
        
        # Parse images from Shopify data
        images = product.get('images', [])
        for img in images:
            if isinstance(img, str):
                product_data['images'].append(upgrade_shopify_image_url(img))
            elif isinstance(img, dict) and img.get('src'):
                product_data['images'].append(upgrade_shopify_image_url(img['src']))
        
        # Parse product type/vendor as brand
        if not product_data['brand']:
            product_data['brand'] = product.get('vendor', '')
        
        # Parse tags
        tags = product.get('tags', [])
        if tags:
            product_data['specifications']['Tags'] = ', '.join(tags) if isinstance(tags, list) else str(tags)
        
        # Parse product type
        if product.get('product_type'):
            product_data['specifications']['Product Type'] = product.get('product_type')
        
        # Parse collections
        if product.get('collections'):
            collections = product.get('collections')
            if isinstance(collections, list):
                product_data['specifications']['Collections'] = ', '.join(collections)
    
    def _apply_meta_fallbacks(self, meta_data: Dict, product_data: Dict):
        """Apply meta tag data as fallbacks."""
        if not product_data['name'] and meta_data.get('title'):
            product_data['name'] = meta_data['title']
        
        if not product_data['description'] and meta_data.get('description'):
            product_data['description'] = self._clean_description(meta_data['description'])
            product_data['description_html'] = f"<p>{meta_data['description']}</p>"
        
        if not product_data['images'] and meta_data.get('image'):
            product_data['images'].append(meta_data['image'])
        
        if not product_data['price'] and meta_data.get('price'):
            product_data['price'] = meta_data['price']
        
        if not product_data['currency'] and meta_data.get('currency'):
            product_data['currency'] = meta_data['currency']
        
        if not product_data['brand'] and meta_data.get('brand'):
            product_data['brand'] = meta_data['brand']
    
    def _extract_specifications(self, html: str, product_data: Dict):
        """Extract product specifications from HTML."""
        # Look for specifications tables/lists
        spec_patterns = [
            r'<div[^>]*class=["\'][^"\']*product-specs[^"\']*["\'][^>]*>(.*?)</div>',
            r'<table[^>]*class=["\'][^"\']*spec[^"\']*["\'][^>]*>(.*?)</table>',
            r'<div[^>]*id=["\']specifications["\'][^>]*>(.*?)</div>',
        ]
        
        for pattern in spec_patterns:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                # Parse table rows if it's a table
                rows = re.findall(r'<tr[^>]*>.*?<td[^>]*>(.*?)</td>.*?<td[^>]*>(.*?)</td>.*?</tr>', match.group(1), re.DOTALL | re.IGNORECASE)
                for label, value in rows:
                    label = re.sub(r'<[^>]+>', '', label).strip()
                    value = re.sub(r'<[^>]+>', '', value).strip()
                    if label and value:
                        product_data['specifications'][label] = value
                break
    
    def _extract_reviews(self, html: str, product_data: Dict):
        """Extract customer reviews if available."""
        # Look for review data in JSON-LD
        reviews_match = re.search(r'"review":\s*(\[[^\]]*\])', html)
        if reviews_match:
            try:
                reviews = json.loads(reviews_match.group(1))
                for review in reviews:
                    if isinstance(review, dict):
                        product_data['reviews'].append({
                            'author': review.get('author', {}).get('name', 'Anonymous'),
                            'rating': review.get('reviewRating', {}).get('ratingValue', 0),
                            'title': review.get('name', ''),
                            'body': review.get('reviewBody', ''),
                            'date': review.get('datePublished', '')
                        })
            except json.JSONDecodeError:
                pass
        
        # Alternative: Look for Shopify review apps (like Judge.me, Loox, etc.)
        review_scripts = re.findall(r'<script[^>]*>.*?"reviews":\s*(\[.*?\]).*?</script>', html, re.DOTALL)
        for script in review_scripts[:1]:  # Just check first match
            try:
                reviews = json.loads(script)
                if isinstance(reviews, list):
                    for review in reviews[:10]:  # Limit to 10 reviews
                        if isinstance(review, dict):
                            product_data['reviews'].append({
                                'author': review.get('reviewer', {}).get('name', 'Anonymous'),
                                'rating': review.get('rating', 0),
                                'title': review.get('title', ''),
                                'body': review.get('body', ''),
                                'date': review.get('created_at', '')
                            })
            except:
                pass
    
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
    
    def _clean_description(self, description: str) -> str:
        """Clean HTML description to plain text."""
        return clean_html_to_text(description)


# Convenience function
def scrape_shopify(url: str) -> Dict[str, Any]:
    """Scrape a Shopify product URL."""
    scraper = ShopifyScraper()
    return scraper.scrape(url)
