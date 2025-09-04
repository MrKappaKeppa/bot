import logging
import random
import requests
import tempfile
import os
import urllib.parse
import subprocess
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater, CommandHandler, CallbackContext, CallbackQueryHandler, MessageHandler, Filters
)
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import time

# Bot token (load from environment variable for security)
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8132949424:AAGUkEaqANjY9ARny2c3HunJjxODoaIy2iU')
# Keep track of sent GIF URLs per user (or globally)
SENT_VIDEOS = {}  # {user_id_or_chat_id: set([video_url1, video_url2, ...])}

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Predefined categories
CATEGORIES = [
    'milf', 'blonde', 'anal', 'cosplay', 'asian', 'lesbian', 'public', 'amateur', 'big tits', 'cumshot', 'solo'
]


def browse(update: Update, context: CallbackContext):
    """
    Show a single menu with modes (Top, Trending, Random) + Categories
    """
    keyboard = [
        [InlineKeyboardButton("🔥 Top", callback_data="mode_top"),
         InlineKeyboardButton("⚡ Trending", callback_data="mode_trending"),
         InlineKeyboardButton("🎲 Random", callback_data="mode_random")]
    ]

    # Add categories in a separate row each
    for cat in CATEGORIES:
        keyboard.append([InlineKeyboardButton(cat.title(), callback_data=f"category_{cat}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("Choose what you want to watch:", reply_markup=reply_markup)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.HTTPError, requests.exceptions.SSLError, requests.exceptions.ConnectionError))
)
def get_redgifs_token():
    try:
        response = requests.get('https://api.redgifs.com/v2/auth/temporary', timeout=10)
        response.raise_for_status()
        data = response.json()
        token = data.get('token')
        if not token:
            logging.error("No token found in response")
            return None
        return token
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to get Redgifs token: {e}")
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting token: {e}")
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.HTTPError, requests.exceptions.SSLError, requests.exceptions.ConnectionError))
)
def search_redgifs(query, token, count=10):
    headers = {'Authorization': f'Bearer {token}'}
    encoded_query = urllib.parse.quote_plus(query)
    url = f'https://api.redgifs.com/v2/gifs/search?search_text={encoded_query}&count={count}'
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '')
        logging.info(f"Response Content-Type: {content_type}")
        logging.info(f"Response Content: {response.text[:500]}")
        if 'application/json' not in content_type.lower():
            logging.error(f"Unexpected Content-Type: {content_type}")
            return {}
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"HTTP error: {e}")
        return {}
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        return {}


def search_user_redgifs(username, token, count=10):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.redgifs.com/v2/users/{username}/search?count={count}"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 404:
            logging.warning(f"User not found: {username}")
            return {"gifs": []}
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logging.error(f"User search failed for {username}: {e}")
        return {"gifs": []}

MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 MB
def send_gif_from_results(target, gifs, title="GIF"):
    chat_id = None
    if hasattr(target, "message"):
        chat_id = target.message.chat_id
    elif hasattr(target, "chat_id"):
        chat_id = target.chat_id

    if chat_id not in SENT_VIDEOS:
        SENT_VIDEOS[chat_id] = set()

    valid_gifs = [gif for gif in gifs if gif.get('duration', 0) > 1]
    if not valid_gifs:
        text = f"No valid GIFs found for {title}."
        if hasattr(target, "edit_message_text"):
            target.edit_message_text(text)
        else:
            target.reply_text(text)
        return

    # Filter out already sent URLs
    gifs = [gif for gif in valid_gifs if gif['urls'].get('mp4') not in SENT_VIDEOS[chat_id]]
    if not gifs:
        text = f"All GIFs for {title} were already sent."
        if hasattr(target, "edit_message_text"):
            target.edit_message_text(text)
        else:
            target.reply_text(text)
        return

    for gif in random.sample(gifs, len(gifs)):
        urls = [gif['urls'].get(x) for x in ["hd", "sd", "mp4", "gif"] if gif['urls'].get(x)]
        gif_url = None
        for url in urls:
            try:
                if url in SENT_VIDEOS[chat_id]:
                    continue  # skip already sent
                head = requests.head(url, allow_redirects=True, timeout=5, verify=False)
                head.raise_for_status()
                size = int(head.headers.get("Content-Length", 0))
                if size <= MAX_FILE_SIZE:
                    gif_url = url
                    break
            except:
                continue

        if not gif_url:
            continue

        temp_path, duration = download_and_validate_gif(gif_url)
        if not temp_path:
            continue

        fixed_path = remux_mp4(temp_path)

        try:
            with open(fixed_path, "rb") as f:
                caption = f"Here’s something from *{title}* 🔥"
                if hasattr(target, "edit_message_text"):
                    target.edit_message_text(f"Sending GIF for {title}...")
                    target.message.reply_video(video=f, supports_streaming=True, duration=int(duration), caption=caption, parse_mode="Markdown")
                else:
                    target.reply_video(video=f, supports_streaming=True, duration=int(duration), caption=caption, parse_mode="Markdown")

            # Mark this URL as sent
            SENT_VIDEOS[chat_id].add(gif_url)

            os.unlink(temp_path)
            if fixed_path != temp_path:
                os.unlink(fixed_path)
            return
        except Exception as e:
            logging.error(f"Failed to send video: {e}")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            if os.path.exists(fixed_path) and fixed_path != temp_path:
                os.unlink(fixed_path)
            continue

    text = f"No suitable new GIFs under {MAX_FILE_SIZE/1024/1024:.0f} MB for {title}."
    if hasattr(target, "edit_message_text"):
        target.edit_message_text(text)
    else:
        target.reply_text(text)


def is_url_reachable(url):
    try:
        response = requests.head(url, allow_redirects=True, timeout=5)
        response.raise_for_status()
        content_length = int(response.headers.get('Content-Length', '0'))
        if content_length > MAX_FILE_SIZE:
            logging.warning(f"Skipping {url}, too large: {content_length/1024/1024:.2f} MB")
            return False
        content_type = response.headers.get('Content-Type', '').lower()
        return content_type in ['image/gif', 'video/mp4']
    except Exception as e:
        logging.warning(f"URL check failed: {e}")
        return False


def get_file_duration(file_path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'json', file_path],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        duration = float(data['format']['duration'])
        return duration
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        logging.error(f"Failed to get duration for {file_path}: {e}")
        return 0
    except FileNotFoundError:
        logging.error("ffprobe not found. Please ensure FFmpeg is installed and in your PATH.")
        return 0


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException,))
)
def download_and_validate_gif(gif_url, max_retries=3):
    """
    Download GIF and validate it has valid size and duration.
    Returns: (path_to_file, duration) or (None, 0) on failure.
    """
    for attempt in range(max_retries):
        try:
            logging.info(f"Attempt {attempt + 1}/{max_retries}: Downloading {gif_url}")
            response = requests.get(gif_url, stream=True, timeout=10)
            response.raise_for_status()

            total_size = int(response.headers.get('Content-Length', '0'))
            if total_size == 0:
                raise Exception("Zero Content-Length")

            # Determine file extension based on Content-Type
            content_type = response.headers.get('Content-Type', '').lower()
            extension = '.mp4' if 'video/mp4' in content_type else '.gif'

            with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as temp_file:
                downloaded_size = 0
                for chunk in response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
                    downloaded_size += len(chunk)
                temp_file_path = temp_file.name

            if downloaded_size != total_size:
                logging.error(f"Incomplete download: {downloaded_size}/{total_size}")
                os.unlink(temp_file_path)
                continue

            # Validate duration
            duration = get_file_duration(temp_file_path)
            if duration <= 1:
                logging.warning(f"Invalid duration ({duration}s), retrying...")
                os.unlink(temp_file_path)
                continue

            return temp_file_path, duration

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return None, 0
            time.sleep(2 ** attempt)

    return None, 0


def start(update: Update, context: CallbackContext):
    browse(update, context)


def categories(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton(cat.title(), callback_data=cat)] for cat in CATEGORIES]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("Choose a category:", reply_markup=reply_markup)


def search_command(update: Update, context: CallbackContext):
    token = get_redgifs_token()
    if not token:
        update.effective_message.reply_text("❌ Failed to authenticate. Try later.")
        return

    if not context.args:
        update.effective_message.reply_text("Usage: /search <keyword>\nExample: /search milf or /search eva elfie")
        return

    query = " ".join(context.args)  # support multi-word queries
    update.effective_message.reply_text(f"🔎 Searching for *{query}*...", parse_mode="Markdown")

    # 1. Try tag search
    results = search_redgifs(query, token, count=10)
    gifs = results.get("gifs", [])

    # 2. If no results, try user search
    if not gifs:
        logging.info(f"No tag results for '{query}', trying user search...")
        results = search_user_redgifs(query.replace(" ", "").lower(), token, count=10)
        gifs = results.get("gifs", [])

    if not gifs:
        update.effective_message.reply_text(f"❌ No results found for '{query}'.")
        return

    send_gif_from_results(update.effective_message, gifs, title=query.title())


def user_command(update: Update, context: CallbackContext):
    token = get_redgifs_token()
    if not token:
        update.message.reply_text("❌ Failed to authenticate. Try later.")
        return

    if not context.args:
        update.message.reply_text("Usage: /user <username>\nExample: /user evaelfie")
        return

    username = context.args[0].lower()
    update.effective_message.reply_text(f"👤 Fetching gifs from user *{username}*...", parse_mode="Markdown")

    results = search_user_redgifs(username, token, count=10)
    gifs = results.get("gifs", [])
    if not gifs:
        update.message.reply_text(f"❌ No gifs found for user '{username}'.")
        return

    send_gif_from_results(update.effective_message, gifs, title=f"User: {username}")


def remux_mp4(input_path):
    """Re-mux MP4 to fix metadata so Telegram shows duration."""
    try:
        output_path = input_path.replace(".mp4", "_fixed.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_path, "-c", "copy", "-movflags", "+faststart", output_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return output_path
    except Exception as e:
        logging.error(f"Remux failed: {e}")
        return input_path


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException,))
)
def trending_redgifs(token, count=10):
    trending_tags = ["milf", "lesbian", "asian", "blonde"]
    query = random.choice(trending_tags)
    return search_redgifs(query, token, count)


def top_redgifs(token, count=10):
    top_tags = ["public", "amateur", "big tits", "cumshot"]
    query = random.choice(top_tags)
    return search_redgifs(query, token, count)


def random_redgifs(token, count=1):
    query = random.choice(CATEGORIES)
    return search_redgifs(query, token, count)


def browse_selected(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    token = get_redgifs_token()
    if not token:
        query.edit_message_text("Failed to authenticate. Try later.")
        return

    mode = query.data
    title = ""
    results = {}

    if mode == "mode_top":
        results = top_redgifs(token)
        title = "Top GIFs"
    elif mode == "mode_trending":
        results = trending_redgifs(token)
        title = "Trending GIFs"
    elif mode == "mode_random":
        results = random_redgifs(token)
        title = "Random GIFs"
    elif mode.startswith("category_"):
        category_name = mode.split("_", 1)[1]
        results = search_redgifs(category_name, token, count=10)
        title = category_name.title()
    else:
        query.edit_message_text("Unknown option.")
        return

    gifs = results.get("gifs", [])
    send_gif_from_results(query, gifs, title)


def handle_message(update: Update, context: CallbackContext):
    update.message.reply_text("Type /categories to browse GIFs 🔞")


def main():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("categories", categories))
    dp.add_handler(CallbackQueryHandler(browse_selected, pattern="^(mode_|category_)"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(CommandHandler("search", search_command))
    dp.add_handler(CommandHandler("user", user_command))

    updater.start_polling()
    updater.idle()


if __name__ == '__main__':
    main()