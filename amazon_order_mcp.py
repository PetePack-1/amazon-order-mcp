#!/usr/bin/env python3
"""MCP server wrapper for amazon-orders library."""

import os
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from urllib.parse import urlencode, urlparse

# Fix asyncio nested event loop issue
import nest_asyncio
nest_asyncio.apply()

from mcp.server.fastmcp import FastMCP
from dateutil import parser as date_parser
from dotenv import load_dotenv

from amazonorders.conf import AmazonOrdersConfig
from amazonorders import util
from amazonorders.constants import Constants
from amazonorders.entity.item import Item
from amazonorders.entity.order import Order
from amazonorders.session import AmazonSession, IODefault
from amazonorders.orders import AmazonOrders

# Load .env from current directory if it exists
load_dotenv()

# Initialize MCP server
mcp = FastMCP(name="amazon-orders")

# Playwright-based solvers for Amazon's JavaScript login challenges (requires the [browser] extra)
BROWSER_AUTH_FORMS = [
    "amazonorders.contrib.browser.playwright.PlaywrightAcicForm",
    "amazonorders.contrib.browser.playwright.PlaywrightJSAuthForm",
    "amazonorders.contrib.browser.playwright.PlaywrightManualWafForm",
]

# Store-specific sign-in handle and auth cookie; amazon-orders only sets these for amazon.com
REGION_AUTH = {
    "de": ("deflex", "x-acbde"),
    "co.uk": ("gbflex", "x-acbuk"),
    "fr": ("frflex", "x-acbfr"),
    "it": ("itflex", "x-acbit"),
    "es": ("esflex", "x-acbes"),
}


class RegionalConstants(Constants):
    """Constants that also adjust sign-in parameters for non-US Amazon stores."""

    def __init__(self, config: Optional[AmazonOrdersConfig] = None) -> None:
        super().__init__(config)
        # The class default is read at import time, before load_dotenv() runs
        if os.environ.get("AMAZON_CURRENCY_SYMBOL"):
            self.CURRENCY_SYMBOL = os.environ["AMAZON_CURRENCY_SYMBOL"]

    def _apply_domain(self, domain: str) -> None:
        super()._apply_domain(domain)

        host = urlparse(self.BASE_URL).netloc.lower().removeprefix("www.")
        region = REGION_AUTH.get(host.removeprefix("amazon."))
        if region is None:
            return

        assoc_handle, auth_cookie = region
        self.SIGN_IN_QUERY_PARAMS = {**self.SIGN_IN_QUERY_PARAMS, "openid.assoc_handle": assoc_handle}
        self.BASE_HEADERS = {**self.BASE_HEADERS,
                             "Referer": f"{self.SIGN_IN_URL}?{urlencode(self.SIGN_IN_QUERY_PARAMS)}"}
        self.COOKIES_SET_WHEN_AUTHENTICATED = [auth_cookie]


class GermanDateInfo(date_parser.parserinfo):
    """dateutil month names as they appear on amazon.de ("17. September 2026", "3. März 2025")."""

    MONTHS = [("Jan", "Januar"), ("Feb", "Februar"), ("Mär", "März", "Mrz"), ("Apr", "April"), ("Mai",),
              ("Jun", "Juni"), ("Jul", "Juli"), ("Aug", "August"), ("Sep", "Sept", "September"),
              ("Okt", "Oktober"), ("Nov", "November"), ("Dez", "Dezember")]


def parse_german_date(value: str):
    try:
        return date_parser.parse(value, fuzzy=True, parserinfo=GermanDateInfo(dayfirst=True)).date()
    except (ValueError, OverflowError):
        return None


# amazon-orders looks up order totals by English label; these are the exact amazon.de labels
GERMAN_CURRENCY_LABELS = {
    "grand total": ["gesamtsumme", "summe"],
    "subtotal": ["zwischensumme"],
    "shipping": ["verpackung & versand"],
    "before tax": ["gesamt vor ust."],
    "estimated tax": ["geschätzte ust."],
}


class GermanParsingMixin:
    """Parse amazon.de's German dates and comma-decimal amounts ("1.234,56 €")."""

    def to_currency(self, value):
        if not isinstance(value, str):
            return super().to_currency(value)

        value = value.strip()
        if value.lower() in ("kostenlos", "gratis"):
            return 0.0

        negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
        number = re.sub(r"[^\d,]", "", value).replace(",", ".")
        if not number:
            return None
        try:
            amount = float(number)
        except ValueError:
            return None
        return -amount if negative else amount

    def simple_parse(self, selector, *args, parse_date=False, **kwargs):
        value = super().simple_parse(selector, *args, **kwargs)
        if parse_date and isinstance(value, str):
            value = parse_german_date(value)
        return value


class GermanItem(GermanParsingMixin, Item):
    pass


class GermanOrder(GermanParsingMixin, Order):
    def _parse_currency(self, contains, combine_multiple=False):
        labels = GERMAN_CURRENCY_LABELS.get(contains)
        if labels is None:
            return None

        selectors = self.config.selectors
        for label in labels:
            value = None
            for tag in util.select(self.parsed, selectors.FIELD_ORDER_SUBTOTALS_TAG_ITERATOR_SELECTOR):
                if (tag.get_text(" ", strip=True).split(":")[0].strip().lower() != label or
                        util.select_one(tag, selectors.FIELD_ORDER_SUBTOTALS_TAG_POPOVER_PRELOAD_SELECTOR)):
                    continue
                inner_tag = util.select_one(tag, selectors.FIELD_ORDER_SUBTOTALS_INNER_TAG_SELECTOR)
                currency = self.to_currency(inner_tag.text) if inner_tag else None
                if currency is not None:
                    value = (value or 0.0) + currency
                    if not combine_multiple:
                        break
            if value is not None:
                return value

        # Order history cards (e.g. digital orders) only show the total in the header
        if contains == "grand total":
            return self.to_currency(self._header_value("summe"))

        return None

    def simple_parse(self, selector, *args, parse_date=False, **kwargs):
        value = super().simple_parse(selector, *args, parse_date=parse_date, **kwargs)
        # amazon.de order history cards use a header column layout the placed-date selectors don't match
        if value is None and selector == self.config.selectors.FIELD_ORDER_PLACED_DATE_SELECTOR:
            header_date = (self._header_value("bestellung aufgegeben") or
                           self._header_value("abonnement abgerechnet am"))
            if header_date:
                value = parse_german_date(header_date)
        return value

    def _header_value(self, label):
        for item in self.parsed.select(".order-header__header-list-item"):
            spans = item.select("span")
            if len(spans) >= 2 and spans[0].get_text(strip=True).lower() == label:
                return spans[-1].get_text(strip=True)
        return None


def make_config() -> AmazonOrdersConfig:
    config = AmazonOrdersConfig(data={"auth_forms_classes": BROWSER_AUTH_FORMS})
    config.constants = RegionalConstants(config)
    if urlparse(config.constants.BASE_URL).netloc.lower().removeprefix("www.") == "amazon.de":
        config.order_cls = GermanOrder
        config.item_cls = GermanItem
    return config


# Global session cache
_session: Optional[AmazonSession] = None
_orders_client: Optional[AmazonOrders] = None
_pending_otp_request: bool = False


class OTPRequiredError(Exception):
    """Raised when OTP code is needed for authentication."""
    pass


class MCPIOHandler(IODefault):
    """Custom IO handler that captures OTP prompts instead of blocking."""

    def __init__(self, otp_code: Optional[str] = None):
        self.otp_code = otp_code
        self.otp_requested = False

    def prompt(self, msg: str, type: Optional[Any] = None, **kwargs: Any) -> Any:
        # Check if this is an OTP prompt
        if "otp" in msg.lower() or "code" in msg.lower() or "verification" in msg.lower():
            self.otp_requested = True
            if self.otp_code:
                return self.otp_code
            raise OTPRequiredError("OTP_REQUIRED")
        # For other prompts, raise with the message
        raise ValueError(f"Interactive prompt required: {msg}")

    def echo(self, msg: str, **kwargs: Any) -> None:
        pass  # Suppress echo output in MCP context


def get_orders_client(otp_code: Optional[str] = None, debug: bool = False) -> AmazonOrders:
    """Get or create an authenticated AmazonOrders client."""
    global _session, _orders_client, _pending_otp_request

    if _orders_client is not None and not _pending_otp_request:
        return _orders_client

    username = os.environ.get("AMAZON_USERNAME")
    password = os.environ.get("AMAZON_PASSWORD")
    otp_secret = os.environ.get("AMAZON_OTP_SECRET")

    if not username or not password:
        raise ValueError(
            "AMAZON_USERNAME and AMAZON_PASSWORD environment variables must be set. "
            f"Found: username={'set' if username else 'NOT SET'}, password={'set' if password else 'NOT SET'}"
        )

    io_handler = MCPIOHandler(otp_code=otp_code)

    _session = AmazonSession(
        username,
        password,
        otp_secret_key=otp_secret,
        io=io_handler,
        debug=debug,
        config=make_config(),
    )

    try:
        # Login may launch Playwright's sync API, which refuses to run inside the
        # MCP server's asyncio loop, so run it in a worker thread without a loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(_session.login).result()
        _pending_otp_request = False
    except OTPRequiredError:
        _pending_otp_request = True
        raise
    except Exception as e:
        if io_handler.otp_requested:
            _pending_otp_request = True
        raise

    _orders_client = AmazonOrders(_session)
    return _orders_client


@mcp.tool()
def amazon_login(otp_code: Optional[str] = None, debug: bool = False) -> str:
    """
    Login to Amazon. CALL THIS FIRST before using other amazon tools.
    If 2FA is enabled, you must provide the current OTP code from your authenticator app.

    Args:
        otp_code: Your current 6-digit OTP code from your authenticator app (required if 2FA is enabled).
        debug: If True, enable debug mode to see what's happening during auth.

    Returns:
        Success message or error details.
    """
    global _orders_client, _session, _pending_otp_request

    # Reset session to force re-login
    _orders_client = None
    _session = None

    try:
        get_orders_client(otp_code=otp_code, debug=debug)
        return "Successfully logged in to Amazon."
    except OTPRequiredError:
        _pending_otp_request = True
        return (
            "OTP_REQUIRED: Amazon 2FA is enabled and requires a one-time password. "
            "ASK THE USER for their current 6-digit OTP code from their authenticator app, "
            "then call amazon_login again with the otp_code parameter."
        )
    except Exception as e:
        error_msg = str(e)
        # Include full traceback for debugging
        import traceback
        tb = traceback.format_exc()

        if "Authentication attempts exhausted" in error_msg:
            return (
                f"AUTH_FAILED: Could not authenticate with Amazon.\n"
                f"OTP code provided: {'yes' if otp_code else 'no'}\n"
                f"Possible causes:\n"
                f"1. Invalid username/password in environment variables\n"
                f"2. Amazon is blocking automated login (CAPTCHA)\n"
                f"3. 2FA is required - ASK THE USER for their current 6-digit OTP code\n"
                f"\nDebug info:\n{tb if debug else error_msg}"
            )
        return f"Login failed: {error_msg}\n\nTraceback:\n{tb if debug else ''}"


def order_to_dict(order) -> dict:
    """Convert an Order object to a serializable dictionary."""
    result = {
        "order_number": order.order_number,
        "order_placed_date": str(order.order_placed_date) if order.order_placed_date else None,
        "grand_total": str(order.grand_total) if order.grand_total else None,
    }

    # Add optional fields if available
    optional_fields = [
        "subtotal", "shipping_total", "estimated_tax", "total_before_tax",
        "refund_total", "promotion_applied", "coupon_savings",
        "subscription_discount", "multibuy_discount", "amazon_discount",
        "reward_points", "gift_card", "payment_method", "payment_method_last_4"
    ]

    for field in optional_fields:
        value = getattr(order, field, None)
        if value is not None:
            result[field] = str(value)

    # Add recipient info if available
    if hasattr(order, "recipient") and order.recipient:
        result["recipient"] = {
            "name": getattr(order.recipient, "name", None),
            "address": getattr(order.recipient, "address", None),
        }

    # Add items if available
    if hasattr(order, "items") and order.items:
        result["items"] = [item_to_dict(item) for item in order.items]

    return result


def item_to_dict(item) -> dict:
    """Convert an Item object to a serializable dictionary."""
    return {
        "title": item.title,
        "price": str(item.price) if item.price else None,
        "quantity": item.quantity,
        "link": item.link,
        "seller": getattr(item.seller, "name", None) if hasattr(item, "seller") and item.seller else None,
        "condition": getattr(item, "condition", None),
    }


def _handle_auth_error(e: Exception) -> str:
    """Return a helpful error message for auth failures."""
    error_msg = str(e)
    if "OTP_REQUIRED" in error_msg or isinstance(e, OTPRequiredError):
        return (
            "NOT_LOGGED_IN: You must call amazon_login first. "
            "ASK THE USER for their current 6-digit OTP code from their authenticator app, "
            "then call amazon_login with the otp_code parameter."
        )
    if "AMAZON_USERNAME" in error_msg or "AMAZON_PASSWORD" in error_msg:
        return f"CONFIG_ERROR: {error_msg}"
    if "Authentication attempts exhausted" in error_msg:
        return (
            "AUTH_FAILED: Call amazon_login first. If 2FA is enabled, "
            "ASK THE USER for their current 6-digit OTP code from their authenticator app."
        )
    return f"Error: {error_msg}"


@mcp.tool()
def amazon_get_order_history(
    year: Optional[int] = None,
    time_filter: Optional[str] = None,
    full_details: bool = False
) -> str:
    """
    Get Amazon order history. Requires amazon_login to be called first.

    Args:
        year: Specific year to get orders from (e.g., 2024). Defaults to current year.
        time_filter: Alternative to year - use "last30" for past 30 days or "months-3" for past 3 months.
        full_details: If True, fetch additional details for each order (slower but more complete).

    Returns:
        JSON string containing list of orders with their details.
    """
    try:
        client = get_orders_client()
    except Exception as e:
        return _handle_auth_error(e)

    kwargs = {"full_details": full_details}

    if time_filter:
        kwargs["time_filter"] = time_filter
    elif year:
        kwargs["year"] = year
    else:
        kwargs["year"] = datetime.now().year

    orders = client.get_order_history(**kwargs)

    result = [order_to_dict(order) for order in orders]

    return json.dumps(result, indent=2)


@mcp.tool()
def amazon_get_order(order_id: str) -> str:
    """
    Get details for a specific Amazon order. Requires amazon_login to be called first.

    Args:
        order_id: The Amazon order number (e.g., "111-1234567-1234567").

    Returns:
        JSON string containing the order details.
    """
    try:
        client = get_orders_client()
    except Exception as e:
        return _handle_auth_error(e)

    order = client.get_order(order_id)

    return json.dumps(order_to_dict(order), indent=2)


@mcp.tool()
def amazon_search_orders(
    search_term: str,
    year: Optional[int] = None,
    time_filter: Optional[str] = None
) -> str:
    """
    Search Amazon orders by item title. Requires amazon_login to be called first.

    Args:
        search_term: Text to search for in item titles (case-insensitive).
        year: Specific year to search in. Defaults to current year.
        time_filter: Alternative to year - use "last30" or "months-3".

    Returns:
        JSON string containing matching orders.
    """
    try:
        client = get_orders_client()
    except Exception as e:
        return _handle_auth_error(e)

    kwargs = {"full_details": True}  # Need full details to search items

    if time_filter:
        kwargs["time_filter"] = time_filter
    elif year:
        kwargs["year"] = year
    else:
        kwargs["year"] = datetime.now().year

    orders = client.get_order_history(**kwargs)

    search_lower = search_term.lower()
    matching_orders = []

    for order in orders:
        if hasattr(order, "items") and order.items:
            for item in order.items:
                if item.title and search_lower in item.title.lower():
                    matching_orders.append(order)
                    break

    result = [order_to_dict(order) for order in matching_orders]

    return json.dumps(result, indent=2)


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
