"""Scrapers module for product video generator."""
from app.scrapers.shopify import ShopifyScraper, scrape_shopify
from app.scrapers.generic import GenericScraper, scrape_generic

__all__ = [
    'ShopifyScraper',
    'scrape_shopify',
    'GenericScraper', 
    'scrape_generic',
]


def scrape_product(url: str) -> dict:
    """
    Scrape a product URL using the appropriate scraper.
    
    Automatically detects Shopify stores and uses the appropriate scraper.
    Falls back to generic scraper for other platforms.
    
    Args:
        url: Product URL to scrape
        
    Returns:
        Dictionary containing product data or error information
    """
    # First try Shopify scraper (it will detect if it's actually Shopify)
    shopify_scraper = ShopifyScraper()
    
    try:
        if shopify_scraper.is_shopify_url(url):
            result = shopify_scraper.scrape(url)
            if 'error' not in result:
                return result
    except:
        pass
    
    # Fall back to generic scraper
    generic_scraper = GenericScraper()
    return generic_scraper.scrape(url)
