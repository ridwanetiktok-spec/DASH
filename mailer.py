"""
Shared logic for email tracking - GA4 via GitHub redirect page.
"""
import re
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse

LINK_PATTERN = re.compile(r'href=["\'](.*?)["\']', re.IGNORECASE)

# Your GitHub redirect page (has GA4 tag)
GITHUB_REDIRECT_URL = "https://ridwanetiktok-spec.github.io/redirect/"

# Your final landing page
FINAL_LANDING_URL = "https://microsoft-offer.free.nf/"

def add_utm_params(url: str, campaign_source: str = "email", 
                   campaign_medium: str = "email", 
                   campaign_name: str = "campaign") -> str:
    """Add GA4 UTM parameters to a URL."""
    parsed = urlparse(url)
    
    if parsed.scheme in ("mailto", "tel", "javascript") or url.startswith("#"):
        return url
    
    query_params = parse_qs(parsed.query)
    
    utm_params = {
        "utm_source": campaign_source,
        "utm_medium": campaign_medium,
        "utm_campaign": campaign_name,
    }
    
    for key, value in utm_params.items():
        if key not in query_params:
            query_params[key] = [value]
    
    new_query = urlencode(query_params, doseq=True)
    new_url = urlunparse((
        parsed.scheme, parsed.netloc, parsed.path, 
        parsed.params, new_query, parsed.fragment
    ))
    
    return new_url

def build_tracked_html(raw_html: str, campaign_name: str = "email_campaign") -> str:
    """
    Rewrite links to go through GitHub redirect page with GA4 tracking,
    then redirect to final landing page.
    """
    
    def replace_link(match):
        original_url = match.group(1)
        
        # Don't rewrite anchors, mailto, tel, or javascript
        if original_url.startswith(("#", "mailto:", "tel:", "javascript:")):
            return match.group(0)
        
        # Add UTM parameters to the final destination
        url_with_utms = add_utm_params(
            FINAL_LANDING_URL, 
            campaign_source="email",
            campaign_medium="email", 
            campaign_name=campaign_name
        )
        
        # GitHub redirect page will redirect to this URL
        # We pass the final URL as a parameter so the redirect page knows where to go
        redirect_url = f'{GITHUB_REDIRECT_URL}?redirect_to={quote(url_with_utms, safe="")}'
        
        return f'href="{redirect_url}"'
    
    html = LINK_PATTERN.sub(replace_link, raw_html)
    return html
