from aiogram import Router, F, types, Bot, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
import json
from sqlalchemy import select, delete, update, func
from database.db import get_db, AsyncSessionLocal
from database.models import (
    AuthorizedUser,
    RoutingRule,
    ChatCache,
    TranslationLog,
    UserCredits,
    SpamMessageLog,
    SourceExcludedTerms,
)
from bot.services import TranslationService, ChatService, ReportService
from shared.config import CENTS_PER_IMAGE_DEFAULT, CENTS_PER_TEXT_DEFAULT
from shared.credit_service import (
    has_sufficient_balance,
    compute_credit_cost_cents,
    reserve as credit_reserve,
    commit_reservation,
    release_reservation,
)
from shared.settings_service import get_system_settings
import logging
import asyncio
from datetime import datetime, timezone, timedelta
import uuid
from dataclasses import dataclass

router = Router()

logger = logging.getLogger(__name__)


class BlockedUserMiddleware(BaseMiddleware):
    """Stop blocked users from using bot commands, callbacks, and private messages."""

    async def __call__(self, handler, event, data):
        uid = None
        if isinstance(event, types.Message) and event.from_user:
            uid = event.from_user.id
        elif isinstance(event, types.CallbackQuery) and event.from_user:
            uid = event.from_user.id
        if uid is None:
            return await handler(event, data)
        async with AsyncSessionLocal() as session:
            r = await session.execute(select(UserCredits).where(UserCredits.telegram_id == uid))
            uc = r.scalar_one_or_none()
            if uc and getattr(uc, "blocked", False):
                if isinstance(event, types.CallbackQuery):
                    await event.answer("Account suspended.", show_alert=True)
                elif isinstance(event, types.Message):
                    await event.answer("Your account is suspended.")
                return
        return await handler(event, data)


class SendRateLimiter:
    """Token-like limiter for Telegram send constraints."""

    def __init__(self):
        self._global_lock = asyncio.Lock()
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._group_locks: dict[int, asyncio.Lock] = {}
        self._last_global = 0.0
        self._last_chat: dict[int, float] = {}
        self._last_group: dict[int, float] = {}

    async def wait(self, chat_id: int, chat_type: str | None):
        now = asyncio.get_running_loop().time()
        # Global: 30 msg/s => 0.0334s
        async with self._global_lock:
            wait_global = max(0.0, (1 / 30) - (now - self._last_global))
            if wait_global > 0:
                await asyncio.sleep(wait_global)
                now = asyncio.get_running_loop().time()
            self._last_global = now

        if chat_type in ("group", "supergroup", "channel"):
            lock = self._group_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                now = asyncio.get_running_loop().time()
                # Group/channel: 20 msg/min => 3s
                wait_group = max(0.0, 3.0 - (now - self._last_group.get(chat_id, 0.0)))
                if wait_group > 0:
                    await asyncio.sleep(wait_group)
                    now = asyncio.get_running_loop().time()
                self._last_group[chat_id] = now
        else:
            lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                now = asyncio.get_running_loop().time()
                # Individual chat: 1 msg/s
                wait_chat = max(0.0, 1.0 - (now - self._last_chat.get(chat_id, 0.0)))
                if wait_chat > 0:
                    await asyncio.sleep(wait_chat)
                    now = asyncio.get_running_loop().time()
                self._last_chat[chat_id] = now


SEND_LIMITER = SendRateLimiter()


@dataclass
class SourceMessageBatch:
    source_id: int
    source_name: str
    source_link: str | None
    messages: list[types.Message]


_source_message_queue: asyncio.Queue[SourceMessageBatch] = asyncio.Queue()
_source_consumer_started = False
_translation_worker_count = 10

# In-memory live log batches for UI (pooled/success/failed).
_live_batches: list[dict] = []
_live_batches_lock = asyncio.Lock()

# Per-owner lock to prevent credit reserve races when multiple messages hit the same owner concurrently.
_credit_locks: dict[int, asyncio.Lock] = {}

def _owner_credit_lock(owner_id: int) -> asyncio.Lock:
    if owner_id not in _credit_locks:
        _credit_locks[owner_id] = asyncio.Lock()
    return _credit_locks[owner_id]

# --- FSM States ---
class ConfigStates(StatesGroup):
    waiting_for_source_id = State()
    waiting_for_dest_group = State()
    waiting_for_lang = State()
    waiting_for_user_id = State()
    # New states for editing
    waiting_for_new_lang = State()

# --- Keyboards ---
def get_main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Manage Sources", callback_data="menu_rules")],
        [InlineKeyboardButton(text="👤 Manage Users", callback_data="menu_users")],
        [InlineKeyboardButton(text="❌ Close", callback_data="close_menu")]
    ])

def get_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Cancel", callback_data="cancel_action")]
    ])

ITEMS_PER_PAGE = 15

def get_paginated_kb(items: list, page: int, item_callback_prefix: str, back_callback: str, page_callback_prefix: str, item_label_key: str = None, item_id_key: str = None, columns: int = 1):
    """
    Generates a paginated keyboard.
    items: list of objects or dicts
    page: current page (0-indexed)
    item_callback_prefix: prefix for item selection callback (e.g. "sel_dest_")
    back_callback: callback for back button
    page_callback_prefix: prefix for pagination callback (e.g. "page_dest_")
    item_label_key: attribute/key for label (default str(item))
    item_id_key: attribute/key for ID (default item.id or item['id'])
    columns: number of columns for items
    """
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    current_items = items[start:end]
    
    kb_rows = []
    current_row = []
    
    for item in current_items:
        # Resolve Label
        if item_label_key:
            label = getattr(item, item_label_key) if hasattr(item, item_label_key) else item.get(item_label_key)
        else:
            label = str(item)
            
        # Resolve ID
        if item_id_key:
            item_id = getattr(item, item_id_key) if hasattr(item, item_id_key) else item.get(item_id_key)
        else:
            item_id = item.id if hasattr(item, 'id') else item
            
        current_row.append(InlineKeyboardButton(text=label, callback_data=f"{item_callback_prefix}{item_id}"))
        
        if len(current_row) == columns:
            kb_rows.append(current_row)
            current_row = []
            
    if current_row:
        kb_rows.append(current_row)
    
    # Navigation Buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"{page_callback_prefix}{page-1}"))
    
    if end < len(items):
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"{page_callback_prefix}{page+1}"))
        
    if nav_row:
        kb_rows.append(nav_row)
        
    # Extra buttons (Manual, Back)
    kb_rows.append([InlineKeyboardButton(text="🔙 Back", callback_data=back_callback)])
    
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# Common languages for selection
LANGUAGES = {
    "en": "🇺🇸 English",
    "es": "🇪🇸 Spanish",
    "fr": "🇫🇷 French",
    "de": "🇩🇪 German",
    "it": "🇮🇹 Italian",
    "pt": "🇵🇹 Portuguese",
    "ru": "🇷🇺 Russian",
    "zh": "🇨🇳 Chinese",
    "ja": "🇯🇵 Japanese",
    "ko": "🇰🇷 Korean",
    "uk": "🇺🇦 Ukrainian",
    "tr": "🇹🇷 Turkish",
    "ar": "🇸🇦 Arabic",
    "ar": "🇸🇦 Arabic",
    "hi": "🇮🇳 Hindi",
    "none": "🚫 No Translation"
}

def get_language_kb(prefix="lang_"):
    kb_rows = []
    row = []
    for code, name in LANGUAGES.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"{prefix}{code}"))
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    
    kb_rows.append([InlineKeyboardButton(text="✍️ Type Manually", callback_data=f"{prefix}manual")])
    kb_rows.append([InlineKeyboardButton(text="🔙 Cancel", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# --- Command Handlers ---

@router.message(CommandStart())
async def command_start(message: types.Message):
    from shared.config import WEB_APP_URL
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if payload.startswith("pickdest"):
        req = KeyboardButtonRequestChat(
            request_id=1,
            chat_is_channel=False,
        )
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Choose a group", request_chat=req)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            "Tap the button below and pick a <b>group</b> to use as a translation destination.\n\n"
            "The bot must be a member of that group (add it as admin if the app requires access).",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return
    if WEB_APP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Open", web_app=types.WebAppInfo(url=WEB_APP_URL))]
        ])
        await message.answer("Hello! I am your Translator Bot.", reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer("Hello! I am your Translator Bot. (WEB_APP_URL is not set.)")


@router.message(F.chat_shared)
async def handle_chat_shared(message: types.Message, bot: Bot):
    """Native group picker from Mini App \"Browse groups\" flow (KeyboardButtonRequestChat)."""
    if message.chat.type != "private":
        return
    cs = message.chat_shared
    if not cs:
        return
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return
    chat_id = cs.chat_id
    remove_kb = ReplyKeyboardRemove()

    async def reply_no_access(text: str):
        await message.answer(text, reply_markup=remove_kb, parse_mode="HTML")

    try:
        chat = await bot.get_chat(chat_id)
    except Exception:
        await reply_no_access(
            "The bot cannot access this chat. Add the bot to the group first:\n"
            "1. Open the group → Add members → search for this bot → add it.\n"
            "2. Give the bot permission to post (administrator recommended), then try again in the Mini App."
        )
        return

    chat_type = getattr(chat.type, "value", None) or str(chat.type)
    if chat_type not in ("group", "supergroup"):
        await reply_no_access(
            "Please choose a <b>group</b>, not a channel. Open Browse groups again and pick a group."
        )
        return

    try:
        await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception:
        await reply_no_access(
            "You don’t appear to be a member of that group. Join the group first, then try again."
        )
        return

    title = chat.title or (getattr(chat, "username", None) or str(chat_id))
    async for session in get_db():
        await ChatService.update_chat_cache(session, chat_id, title, chat_type)
        break

    await message.answer(
        f"✅ <b>{title}</b> is saved. Return to the Mini App — the list will refresh when you come back.",
        reply_markup=remove_kb,
        parse_mode="HTML",
    )

from aiogram.utils.markdown import hbold, hcode, text as fmt_text
# We are using ParseMode.MARKDOWN (Legacy) or MARKDOWN_V2?
# The code says "Markdown" which usually refers to legacy v1.
# But "MarkdownV2" is recommended and supported by telegramify_markdown.
# The error "Can't find end of entity" suggests V1 or V2 issues.
# Best practice: Use HTML for internal strings if possible to avoid escaping issues entirely, 
# OR use a helper to escape.
# For legacy "Markdown", we need to escape `_`, `*`, `[`, ```.
# Let's switch to HTML for the menus? It's much safer.
# Changing parse_mode="Markdown" to "HTML" involves updating bold syntax from ** to <b>.

# Decision: Switch menus to HTML for robustness against names with underscores.

@router.message(Command("config"))
async def command_config(message: types.Message):
    if not await is_authorized_user(message.from_user.id):
        await message.answer("You are not authorized to configure this bot.")
        return
    from shared.config import WEB_APP_URL
    if WEB_APP_URL:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Open", web_app=types.WebAppInfo(url=WEB_APP_URL))]
        ])
        await message.answer("Open the app to manage sources and destinations.", reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer("WEB_APP_URL is not set.")

# --- Menu Callbacks ---

@router.callback_query(F.data == "close_menu")
async def close_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()

@router.callback_query(F.data == "cancel_action")
async def cancel_action(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ <b>Configuration Menu</b>", reply_markup=get_main_menu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "menu_rules")
async def menu_rules(callback: types.CallbackQuery, bot: Bot):
    await show_rules_page(callback, bot, page=0)

async def show_rules_page(callback: types.CallbackQuery, bot: Bot, page: int):
    async for session in get_db():
        # Get unique sources
        result = await session.execute(select(RoutingRule.source_id).distinct())
        sources = result.scalars().all()
        
        # Pagination logic
        start = page * 15
        end = start + 15
        current_source_ids = sources[start:end]
        
        kb_rows = []
        current_row = []
        
        from html import escape
        
        for source_id in current_source_ids:
            # Try to resolve source name
            chat_res = await session.execute(select(ChatCache).where(ChatCache.id == source_id))
            chat_cache = chat_res.scalar_one_or_none()
            
            source_name = f"ID: {source_id}"
            if chat_cache and chat_cache.title:
                source_name = chat_cache.title
            else:
                try:
                    chat_info = await bot.get_chat(source_id)
                    title = chat_info.title or chat_info.full_name or chat_info.username
                    if title:
                        source_name = title
                        await ChatService.update_chat_cache(session, source_id, title, chat_info.type)
                except Exception:
                    pass 

            label = f"{source_name}"
            if len(label) > 15:
                label = label[:12] + "..."
                
            current_row.append(InlineKeyboardButton(text=label, callback_data=f"view_source_{source_id}"))
            
            if len(current_row) == 3:
                kb_rows.append(current_row)
                current_row = []
        
        if current_row:
            kb_rows.append(current_row)
        
        # Navigation
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"page_rules_{page-1}"))
        if end < len(sources):
            nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"page_rules_{page+1}"))
        if nav_row:
            kb_rows.append(nav_row)
            
        kb_rows.append([InlineKeyboardButton(text="➕ Add New Source", callback_data="add_rule")])
        kb_rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="cancel_action")])
        
        text = "📋 <b>Manage Sources</b>\nSelect a source to view its destinations:\n\n"
        if not sources:
            text = "No sources found."
            
        if isinstance(callback, types.Message):
             await callback.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
        else:
             await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

@router.callback_query(F.data.startswith("page_rules_"))
async def page_rules_nav(callback: types.CallbackQuery, bot: Bot):
    page = int(callback.data.split("_")[2])
    await show_rules_page(callback, bot, page)

@router.callback_query(F.data == "menu_users")
async def menu_users(callback: types.CallbackQuery):
    async for session in get_db():
        result = await session.execute(select(AuthorizedUser))
        users = result.scalars().all()
        
        text = "👤 <b>Authorized Users</b>\n\n"
        kb_rows = []
        
        from html import escape
        
        for user in users:
            safe_username = escape(user.username or 'No User')
            text += f"• <code>{user.telegram_id}</code> ({safe_username})\n"
            kb_rows.append([InlineKeyboardButton(text=f"🗑 Del {user.telegram_id}", callback_data=f"del_user_{user.id}")])
        
        kb_rows.append([InlineKeyboardButton(text="➕ Add User", callback_data="add_user")])
        kb_rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="cancel_action")])
        
        await callback.message.edit_text(text or "No users found.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

@router.callback_query(F.data.startswith("view_source_"))
async def view_source(callback: types.CallbackQuery):
    source_id = int(callback.data.split("_")[2])
    await show_source_page(callback, source_id)

async def show_source_page(callback: types.CallbackQuery, source_id: int):
    async for session in get_db():
        result = await session.execute(select(RoutingRule).where(RoutingRule.source_id == source_id))
        rules = result.scalars().all()
        
        text = f"📡 <b>Source: {source_id}</b>\nDestinations:\n\n"
        kb_rows = []
        
        from html import escape
        
        for rule in rules:
            chat_res = await session.execute(select(ChatCache).where(ChatCache.id == rule.destination_group_id))
            chat = chat_res.scalar_one_or_none()
            dest_name = chat.title if chat else str(rule.destination_group_id)
            
            safe_dest_name = escape(dest_name)
            
            text += f"• {safe_dest_name} ({rule.destination_language})\n"
            kb_rows.append([InlineKeyboardButton(text=f"⚙️ {dest_name}", callback_data=f"view_rule_{rule.id}")])
            
        kb_rows.append([InlineKeyboardButton(text="➕ Add Destination", callback_data=f"add_dest_to_{source_id}")])
        kb_rows.append([InlineKeyboardButton(text="🗑 Delete Source", callback_data=f"del_source_{source_id}")])
        kb_rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu_rules")])
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

@router.callback_query(F.data.startswith("view_rule_"))
async def view_rule(callback: types.CallbackQuery):
    rule_id = int(callback.data.split("_")[2])
    
    async for session in get_db():
        result = await session.execute(select(RoutingRule).where(RoutingRule.id == rule_id))
        rule = result.scalar_one_or_none()
        
        if not rule:
            await callback.answer("Rule not found.")
            await menu_rules(callback, callback.bot) # Pass bot
            return

        chat_res = await session.execute(select(ChatCache).where(ChatCache.id == rule.destination_group_id))
        chat = chat_res.scalar_one_or_none()
        dest_name = chat.title if chat else str(rule.destination_group_id)
        
        from html import escape
        safe_dest_name = escape(dest_name)
        
        text = (
            f"📍 <b>Destination Details</b>\n"
            f"Source: <code>{rule.source_id}</code>\n"
            f"Group: {safe_dest_name}\n"
            f"Language: <code>{rule.destination_language}</code>"
        )
        
        kb_rows = [
            [InlineKeyboardButton(text="✏️ Edit Language", callback_data=f"edit_lang_{rule.id}")],
            [InlineKeyboardButton(text="🗑 Delete Destination", callback_data=f"del_rule_{rule.id}")],
            [InlineKeyboardButton(text="🔙 Back", callback_data=f"view_source_{rule.source_id}")]
        ]
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")

# --- Add Rule Flow ---

@router.callback_query(F.data == "add_rule")
async def start_add_rule(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(owner_telegram_id=callback.from_user.id)
    await state.set_state(ConfigStates.waiting_for_source_id)
    await callback.message.edit_text(
        "1️⃣ **Enter Source ID**\n"
        "Send the Telegram ID of the user or channel to listen to.\n"
        "Example: `-100123456789` or `12345678`",
        reply_markup=get_cancel_kb(),
        parse_mode="Markdown"
    )

@router.my_chat_member()
async def on_chat_member_update(event: types.ChatMemberUpdated):
    chat = event.chat
    logger.info(f"Chat member update: {chat.id} - {chat.title} ({chat.type})")
    async for session in get_db():
        await ChatService.update_chat_cache(session, chat.id, chat.title, chat.type)

@router.message(F.chat.type.in_(['group', 'supergroup']))
async def on_group_message(message: types.Message):
    # Also cache on regular messages to ensure we have the chat
    chat = message.chat
    async for session in get_db():
        await ChatService.update_chat_cache(session, chat.id, chat.title, chat.type)

@router.message(ConfigStates.waiting_for_source_id)
async def process_source_id(message: types.Message, state: FSMContext):
    try:
        source_id = int(message.text.strip())
        await state.update_data(source_id=source_id)
        logger.info(f"Captured source_id: {source_id}")
        
        await show_dest_selection(message, state, page=0)
        
    except ValueError:
        await message.answer("❌ Invalid ID. Please enter a number.")

async def show_dest_selection(message_or_callback, state: FSMContext, page: int):
    # Fetch cached chats for selection
    async for session in get_db():
        chats = await ChatService.get_all_chats(session)
    
    # Filter groups
    group_chats = [c for c in chats if c.type in ['group', 'supergroup']]
    
    # Prepare items for helper
    # We need to pass a list of dicts or objects with 'title' and 'id'
    # ChatCache objects have these.
    # We also need to add "Manual" button.
    
    kb = get_paginated_kb(
        items=group_chats,
        page=page,
        item_callback_prefix="sel_dest_",
        back_callback="cancel_action",
        page_callback_prefix="page_dest_",
        item_label_key="title",
        item_id_key="id",
        columns=3
    )
    
    # Add Manual Button manually (insert before back)
    # The helper adds Back at the end.
    # We can modify the kb.inline_keyboard
    
    manual_btn = [InlineKeyboardButton(text="✍️ Enter Group ID Manually", callback_data="manual_dest_id")]
    # Insert before the last row (Back)
    if kb.inline_keyboard:
        kb.inline_keyboard.insert(-1, manual_btn)
    else:
        kb.inline_keyboard.append(manual_btn)
        
    text = (
        "2️⃣ **Select Destination Group**\n"
        "Choose a group where the bot is a member, or enter the ID manually:"
    )
    
    await state.set_state(ConfigStates.waiting_for_dest_group)
    
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("page_dest_"), ConfigStates.waiting_for_dest_group)
async def page_dest_nav(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[2])
    await show_dest_selection(callback, state, page)

@router.callback_query(F.data == "manual_dest_id", ConfigStates.waiting_for_dest_group)
async def manual_dest_id_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ **Enter Destination Group ID**\n"
        "Please send the Group ID (e.g., `-100123456789`).\n"
        "*Tip*: You can get this by forwarding a message from the group to @userinfobot or similar tools.",
        reply_markup=get_cancel_kb(),
        parse_mode="Markdown"
    )

@router.message(ConfigStates.waiting_for_dest_group)
async def process_manual_dest_id(message: types.Message, state: FSMContext):
    try:
        dest_id = int(message.text.strip())
        await state.update_data(dest_id=dest_id)
        logger.info(f"Captured manual dest_id: {dest_id}")
        
        await state.set_state(ConfigStates.waiting_for_lang)
        await message.answer(
            "3️⃣ **Select Target Language**\n"
            "Choose from the list or type manually:",
            reply_markup=get_language_kb(prefix="set_lang_"),
            parse_mode="Markdown"
        )
    except ValueError:
        await message.answer("❌ Invalid ID. Please enter a number.")

@router.callback_query(F.data.startswith("sel_dest_"), ConfigStates.waiting_for_dest_group)
async def process_dest_group(callback: types.CallbackQuery, state: FSMContext):
    dest_id = int(callback.data.split("_")[2])
    await state.update_data(dest_id=dest_id)
    logger.info(f"Captured dest_id: {dest_id}")
    
    await state.set_state(ConfigStates.waiting_for_lang)
    await callback.message.edit_text(
        "3️⃣ **Select Target Language**\n"
        "Choose from the list or type manually:",
        reply_markup=get_language_kb(prefix="set_lang_"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("set_lang_"), ConfigStates.waiting_for_lang)
async def process_lang_selection(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[2]
    if code == "manual":
        await callback.message.edit_text("✍️ **Enter Language Code**\nExample: `es`, `fr`, `de`", reply_markup=get_cancel_kb(), parse_mode="Markdown")
        return # Stay in waiting_for_lang state
    
    # Process selected language
    await finalize_add_rule(callback.message, state, code)

@router.message(ConfigStates.waiting_for_lang)
async def process_lang_manual(message: types.Message, state: FSMContext):
    lang = message.text.strip().lower()
    await finalize_add_rule(message, state, lang)

async def finalize_add_rule(message: types.Message, state: FSMContext, lang: str):
    data = await state.get_data()
    logger.info(f"Captured lang: {lang}, Data: {data}")
    owner_id = data.get("owner_telegram_id")
    if owner_id is None and not getattr(message.from_user, "is_bot", True):
        owner_id = message.from_user.id
    try:
        async for session in get_db():
            rule = RoutingRule(
                source_id=data["source_id"],
                destination_group_id=data["dest_id"],
                destination_language=lang,
                translate_images=False,
                owner_telegram_id=owner_id,
            )
            session.add(rule)
            await session.commit()
        
        await state.clear()
        # If message is from callback (has edit_text), use it, else answer
        if isinstance(message, types.Message):
            await message.answer("✅ **Rule Added Successfully!**", reply_markup=get_main_menu_kb(), parse_mode="Markdown")
        else:
             # It's actually a message object even if from callback.message
             await message.edit_text("✅ **Rule Added Successfully!**", reply_markup=get_main_menu_kb(), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error saving rule: {e}")
        if isinstance(message, types.Message):
            await message.answer(f"❌ Error saving rule: {e}")

# --- Edit Language Flow ---

@router.callback_query(F.data.startswith("edit_lang_"))
async def start_edit_lang(callback: types.CallbackQuery, state: FSMContext):
    rule_id = int(callback.data.split("_")[2])
    await state.update_data(edit_rule_id=rule_id)
    
    await state.set_state(ConfigStates.waiting_for_new_lang)
    await callback.message.edit_text(
        "3️⃣ **Select New Target Language**\n"
        "Choose from the list or type manually:",
        reply_markup=get_language_kb(prefix="new_lang_"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("new_lang_"), ConfigStates.waiting_for_new_lang)
async def process_new_lang_selection(callback: types.CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[2]
    if code == "manual":
        await callback.message.edit_text("✍️ **Enter Language Code**\nExample: `es`, `fr`, `de`", reply_markup=get_cancel_kb(), parse_mode="Markdown")
        return
    
    await finalize_edit_lang(callback.message, state, code)

@router.message(ConfigStates.waiting_for_new_lang)
async def process_new_lang_manual(message: types.Message, state: FSMContext):
    lang = message.text.strip().lower()
    await finalize_edit_lang(message, state, lang)

async def finalize_edit_lang(message: types.Message, state: FSMContext, lang: str):
    data = await state.get_data()
    rule_id = data.get('edit_rule_id')
    
    try:
        async for session in get_db():
            rule = await session.get(RoutingRule, rule_id)
            if rule:
                rule.destination_language = lang
                await session.commit()
                msg_text = f"✅ Language updated to `{lang}`!"
            else:
                msg_text = "❌ Rule not found."
        
        await state.clear()
        if isinstance(message, types.Message):
             # Try to edit if it was a callback message (but here we might have a text message from manual input)
             # If manual input, we answer. If callback selection, we edit.
             # Actually finalize is called with message object.
             # If it was manual input, message is the user's message. We should answer.
             # If it was callback, message is the bot's message. We should edit.
             # How to distinguish? Check message.from_user.is_bot
             if message.from_user.is_bot:
                 await message.edit_text(msg_text, reply_markup=get_main_menu_kb(), parse_mode="Markdown")
             else:
                 await message.answer(msg_text, reply_markup=get_main_menu_kb(), parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error updating rule: {e}")
        if not message.from_user.is_bot:
            await message.answer(f"❌ Error updating rule: {e}")

@router.callback_query(F.data.startswith("add_dest_to_"))
async def start_add_dest_to_source(callback: types.CallbackQuery, state: FSMContext):
    source_id = int(callback.data.split("_")[3])
    await state.update_data(source_id=source_id)
    logger.info(f"Starting add dest for existing source: {source_id}")
    
    # Use the same shared function
    await show_dest_selection(callback, state, page=0)


# --- Add User Flow ---

@router.callback_query(F.data == "add_user")
async def start_add_user(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ConfigStates.waiting_for_user_id)
    await callback.message.edit_text(
        "👤 **Enter User ID**\n"
        "Send the Telegram ID of the user to authorize.",
        reply_markup=get_cancel_kb(),
        parse_mode="Markdown"
    )

@router.message(ConfigStates.waiting_for_user_id)
async def process_user_id(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
        async for session in get_db():
            user = AuthorizedUser(telegram_id=user_id)
            session.add(user)
            await session.commit()
        
        await state.clear()
        await message.answer("✅ **User Authorized!**", reply_markup=get_main_menu_kb(), parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid ID. Please enter a number.")

# --- Delete Handlers ---

# --- Delete Handlers with Confirmation ---

def get_confirm_kb(action: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Yes", callback_data=f"confirm_{action}"),
            InlineKeyboardButton(text="❌ No", callback_data="cancel_action")
        ]
    ])

@router.callback_query(F.data.startswith("del_source_"))
async def delete_source(callback: types.CallbackQuery):
    source_id = callback.data.split("_")[2]
    await callback.message.edit_text(
        f"⚠️ **Are you sure you want to delete source `{source_id}`?**\n"
        "This will delete all routing rules for this source.",
        reply_markup=get_confirm_kb(f"del_source_{source_id}"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("confirm_del_source_"))
async def confirm_delete_source(callback: types.CallbackQuery):
    source_id = int(callback.data.split("_")[3])
    async for session in get_db():
        await session.execute(delete(RoutingRule).where(RoutingRule.source_id == source_id))
        await session.commit()
    
    await callback.answer("Source deleted.")
    await menu_rules(callback, callback.bot) # Pass bot explicitly if needed, but menu_rules expects it from dependency injection if defined, or we can just call it. 
    # Wait, menu_rules signature is (callback: types.CallbackQuery, bot: Bot). 
    # We need to pass bot.
    # Actually, handlers are usually called by dispatcher with dependencies. calling manually requires passing them.
    # Let's fix menu_rules signature or how we call it.
    # menu_rules uses `bot` argument.
    # We can get bot from callback.bot or message.bot
    # But better to just trigger the menu again or edit text.
    # Let's just call menu_rules and hope DI works or pass callback.bot
    await menu_rules(callback, callback.bot)

@router.callback_query(F.data.startswith("del_rule_"))
async def delete_rule(callback: types.CallbackQuery):
    rule_id = callback.data.split("_")[2]
    await callback.message.edit_text(
        f"⚠️ **Are you sure you want to delete this destination?**",
        reply_markup=get_confirm_kb(f"del_rule_{rule_id}"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("confirm_del_rule_"))
async def confirm_delete_rule(callback: types.CallbackQuery):
    rule_id = int(callback.data.split("_")[3])
    source_id = None
    
    async for session in get_db():
        # Get source_id before deleting to return to view
        rule = await session.get(RoutingRule, rule_id)
        if rule:
            source_id = rule.source_id
            await session.delete(rule)
            await session.commit()
            
    await callback.answer("Destination deleted.")
    if source_id:
        await show_source_page(callback, source_id)
    else:
        await menu_rules(callback, callback.bot)

@router.callback_query(F.data.startswith("del_user_"))
async def delete_user(callback: types.CallbackQuery):
    user_id = callback.data.split("_")[2]
    await callback.message.edit_text(
        f"⚠️ **Are you sure you want to remove user `{user_id}`?**",
        reply_markup=get_confirm_kb(f"del_user_{user_id}"),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("confirm_del_user_"))
async def confirm_delete_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[3])
    async for session in get_db():
        await session.execute(delete(AuthorizedUser).where(AuthorizedUser.id == user_id))
        await session.commit()
    
    await callback.answer("User removed.")
    await menu_users(callback) # Refresh list

# --- Message Routing Logic (Existing) ---

# --- Message Routing Logic (Refactored) ---

async def get_routing_rules(source_id: int):
    async for session in get_db():
        stmt = select(RoutingRule).where(RoutingRule.source_id == source_id)
        result = await session.execute(stmt)
        return result.scalars().all()
    return []


async def get_routing_rules_for_session(session, source_id: int):
    stmt = select(RoutingRule).where(
        RoutingRule.source_id == source_id,
        RoutingRule.enabled == True,
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def deactivate_all_rules_for_owner(session, owner_telegram_id: int) -> None:
    """Disable all translation pairs owned by this user (e.g. after insufficient balance)."""
    await session.execute(
        update(RoutingRule).where(RoutingRule.owner_telegram_id == owner_telegram_id).values(enabled=False)
    )


async def deactivate_rules_for_owner_and_source(session, owner_telegram_id: int, source_id: int) -> None:
    """Disable only rules for this owner and source (e.g. after spam threshold)."""
    await session.execute(
        update(RoutingRule).where(
            RoutingRule.owner_telegram_id == owner_telegram_id,
            RoutingRule.source_id == source_id,
        ).values(enabled=False)
    )


async def apply_spam_protection_and_record(
    session, source_id: int, rules: list, bot: Bot, source_name: str
) -> list:
    """
    For owners with spam protection enabled, count messages from this source in their window;
    if over threshold, deactivate rules for that (source, owner), notify owner, and exclude those rules.
    Then insert one SpamMessageLog row per (source_id, owner) for remaining rules.
    Returns the filtered list of rules to process.
    """
    if not rules:
        return rules
    # Prune old rows to avoid unbounded growth (keep last 48 hours)
    prune_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    await session.execute(delete(SpamMessageLog).where(SpamMessageLog.created_at < prune_cutoff))
    distinct_owners = {getattr(r, "owner_telegram_id", None) for r in rules}
    distinct_owners.discard(None)
    if not distinct_owners:
        return rules

    spam_triggered_owners = set()
    for owner_id in distinct_owners:
        stmt = select(UserCredits).where(UserCredits.telegram_id == owner_id)
        r = await session.execute(stmt)
        uc = r.scalar_one_or_none()
        if not uc or not getattr(uc, "spam_protection_enabled", False):
            continue
        max_messages = getattr(uc, "spam_max_messages", 50)
        window_minutes = getattr(uc, "spam_window_minutes", 5)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        count_stmt = select(func.count(SpamMessageLog.id)).where(
            SpamMessageLog.source_id == source_id,
            SpamMessageLog.owner_telegram_id == owner_id,
            SpamMessageLog.created_at >= cutoff,
        )
        count_result = await session.execute(count_stmt)
        count = count_result.scalar() or 0
        if count >= max_messages:
            await deactivate_rules_for_owner_and_source(session, owner_id, source_id)
            spam_triggered_owners.add(owner_id)
            try:
                await bot.send_message(
                    owner_id,
                    "⚠️ <b>Spam protection</b>\n\n"
                    f"Too many messages from source <b>{source_name}</b> (ID: {source_id}) in the last {window_minutes} minutes.\n\n"
                    "Translation pairs from this source have been <b>paused</b>.\n\n"
                    "To resume: open the TeleTranslate app, go to <b>Pairs</b>, and re-enable the pair(s) for this source.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Failed to send spam-protection notice to %s: %s", owner_id, e)

    filtered_rules = [r for r in rules if getattr(r, "owner_telegram_id", None) not in spam_triggered_owners]
    recorded_owners = set()
    for r in filtered_rules:
        owner = getattr(r, "owner_telegram_id", None)
        if owner is not None and owner not in recorded_owners:
            recorded_owners.add(owner)
            session.add(SpamMessageLog(source_id=source_id, owner_telegram_id=owner))
    return filtered_rules


def _cost_cents_for_rule(
    rule: RoutingRule,
    messages: list,
    *,
    cents_per_text: int,
    cents_per_image: int,
) -> int:
    """Cost for this rule in USD cents: 0 if copy mode, else text + per-image rates from system settings.

    Reserved amount assumes each photo may run native image translation. Actual API use also includes a
    lightweight vision YES/NO check before regeneration; that extra call is not billed separately for now.
    """
    if rule.destination_language.lower() == "none":
        return 0
    caption_msg = next((m for m in messages if m.caption), messages[0] if messages else None)
    raw = (caption_msg.text or caption_msg.caption or "") if caption_msg else ""
    is_poll = len(messages) == 1 and getattr(messages[0], "poll", None) is not None
    has_text = bool(raw.strip()) or (is_poll and getattr(rule, "translate_poll", False))
    num_images = sum(1 for m in messages if m.photo) if getattr(rule, "translate_images", False) else 0
    return compute_credit_cost_cents(
        has_text,
        num_images,
        cents_per_text=cents_per_text,
        cents_per_image=cents_per_image,
    )

async def is_authorized_user(user_id: int):
    async for session in get_db():
        stmt = select(AuthorizedUser).where(AuthorizedUser.telegram_id == user_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None
    return False


async def _live_batch_create(source_id: int, source_name: str, source_link: str | None, rules: list[RoutingRule]) -> str:
    batch_id = uuid.uuid4().hex
    row = {
        "batch_id": batch_id,
        "source_id": source_id,
        "source_name": source_name,
        "source_link": source_link,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "destinations": [
            {
                "rule_id": r.id,
                "owner_telegram_id": getattr(r, "owner_telegram_id", None),
                "destination_group_id": r.destination_group_id,
                "status": "Pooled",
                "error_message": None,
                "destination_link": None,
                "cost_usd_cents": None,
            }
            for r in rules
        ],
    }
    async with _live_batches_lock:
        _live_batches.insert(0, row)
        del _live_batches[30:]
    return batch_id


async def _live_batch_update(batch_id: str, rule_id: int, status: str, error: str | None = None, link: str | None = None, cost_usd_cents: int | None = None):
    async with _live_batches_lock:
        for batch in _live_batches:
            if batch["batch_id"] != batch_id:
                continue
            for d in batch["destinations"]:
                if d["rule_id"] == rule_id:
                    d["status"] = status
                    if error is not None:
                        d["error_message"] = error
                    if link is not None:
                        d["destination_link"] = link
                    if cost_usd_cents is not None:
                        d["cost_usd_cents"] = cost_usd_cents
                    return


async def get_live_batches_for_owner(owner_id: int) -> list[dict]:
    async with _live_batches_lock:
        out = []
        for batch in _live_batches:
            dests = [d for d in batch["destinations"] if d.get("owner_telegram_id") == owner_id]
            if not dests:
                continue
            out.append({
                "batch_id": batch["batch_id"],
                "source_id": batch["source_id"],
                "source_name": batch["source_name"],
                "source_link": batch["source_link"],
                "created_at": batch["created_at"],
                "destinations": dests,
            })
        return out


def _log_translation(session, source_id: int, source_link: str | None, rule: RoutingRule, res: dict):
    """Append one TranslationLog row (caller must commit)."""
    dest_link = res.get("link")
    if dest_link and (dest_link == "N/A" or dest_link.startswith("Album")):
        dest_link = None
    cost_cents = res.get("cost_usd_cents")
    log = TranslationLog(
        source_id=source_id,
        destination_group_id=rule.destination_group_id,
        rule_id=rule.id,
        owner_telegram_id=getattr(rule, "owner_telegram_id", None),
        status=res.get("status", "Unknown"),
        source_link=source_link,
        destination_link=dest_link,
        error_message=res.get("error"),
        cost_usd_cents=cost_cents if cost_cents is not None else None,
    )
    session.add(log)


async def load_excluded_terms_for_source(session, source_id: int) -> list[str]:
    row = await session.get(SourceExcludedTerms, source_id)
    if not row:
        return []
    try:
        arr = json.loads(row.terms_json or "[]")
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    return [x for x in arr if isinstance(x, str)]


async def process_rules_for_messages(
    session,
    bot: Bot,
    rules: list,
    messages: list,
    source_id: int,
    source_link: str | None,
    max_message_length: int,
    batch_id: str | None = None,
    *,
    cents_per_text: int = CENTS_PER_TEXT_DEFAULT,
    cents_per_image: int = CENTS_PER_IMAGE_DEFAULT,
) -> tuple[list, set]:
    """Reserve credits first, then process destinations in parallel (up to 10)."""
    excluded_terms = await load_excluded_terms_for_source(session, source_id)
    results = []
    runnable: list[tuple[RoutingRule, int, int | None]] = []
    semaphore = asyncio.Semaphore(_translation_worker_count)

    for rule in rules:
        cost_cents = _cost_cents_for_rule(
            rule,
            messages,
            cents_per_text=cents_per_text,
            cents_per_image=cents_per_image,
        )
        owner = getattr(rule, "owner_telegram_id", None)
        if owner is not None and cost_cents > 0:
            async with _owner_credit_lock(owner):
                if not await has_sufficient_balance(session, owner, cost_cents):
                    cost_usd = cost_cents / 100.0
                    res_skip = {
                        "dest_id": rule.destination_group_id,
                        "dest_name": str(rule.destination_group_id),
                        "status": "Skipped",
                        "error": f"Skipped - insufficient funds (need ${cost_usd:.2f})",
                        "cost_usd_cents": 0,
                    }
                    results.append(res_skip)
                    _log_translation(session, source_id, source_link, rule, res_skip)
                    if batch_id:
                        await _live_batch_update(batch_id, rule.id, "Failed", error=res_skip["error"], cost_usd_cents=0)
                    continue
                ok = await credit_reserve(session, owner, cost_cents)
                if not ok:
                    res_skip = {
                        "dest_id": rule.destination_group_id,
                        "dest_name": str(rule.destination_group_id),
                        "status": "Skipped",
                        "error": "Skipped - insufficient funds",
                        "cost_usd_cents": 0,
                    }
                    results.append(res_skip)
                    _log_translation(session, source_id, source_link, rule, res_skip)
                    if batch_id:
                        await _live_batch_update(batch_id, rule.id, "Failed", error=res_skip["error"], cost_usd_cents=0)
                    continue
        runnable.append((rule, cost_cents, owner))

    async def run_one(rule: RoutingRule, cost_cents: int, owner: int | None):
        async with semaphore:
            if batch_id:
                await _live_batch_update(batch_id, rule.id, "Sending")
            try:
                res = await route_content(
                    bot,
                    messages,
                    rule,
                    source_lang="unknown",
                    max_message_length=max_message_length,
                    excluded_terms=excluded_terms,
                )
                return (rule, owner, cost_cents, res, None)
            except Exception as e:
                return (rule, owner, cost_cents, None, e)

    task_results = await asyncio.gather(
        *[run_one(rule, cost_cents, owner) for (rule, cost_cents, owner) in runnable],
        return_exceptions=False,
    )

    for rule, owner, cost_cents, res, err in task_results:
        if err is not None:
            if owner is not None and cost_cents > 0:
                await release_reservation(session, owner, cost_cents)
            res_fail = {
                "dest_id": rule.destination_group_id,
                "dest_name": str(rule.destination_group_id),
                "status": "Failed",
                "error": str(err),
                "cost_usd_cents": 0,
            }
            results.append(res_fail)
            _log_translation(session, source_id, source_link, rule, res_fail)
            if batch_id:
                await _live_batch_update(batch_id, rule.id, "Failed", error=res_fail["error"], cost_usd_cents=0)
            continue

        res["dest_name"] = str(res["dest_id"])
        if owner is not None and cost_cents > 0:
            if res.get("status") == "Success":
                await commit_reservation(session, owner, cost_cents)
                res["cost_usd_cents"] = cost_cents
            else:
                await release_reservation(session, owner, cost_cents)
                res["cost_usd_cents"] = 0
        else:
            res["cost_usd_cents"] = cost_cents
        results.append(res)
        _log_translation(session, source_id, source_link, rule, res)
        if batch_id:
            await _live_batch_update(
                batch_id,
                rule.id,
                "Success" if res.get("status") == "Success" else "Failed",
                error=res.get("error"),
                link=res.get("link"),
                cost_usd_cents=res.get("cost_usd_cents", 0),
            )
    owners_set = {r.owner_telegram_id for r in rules if getattr(r, "owner_telegram_id", None) is not None}
    return results, owners_set


# --- Flood protection: retry on TelegramRetryAfter ---
async def with_flood_retry(coro_fn, max_retries=2):
    """Run coroutine; on TelegramRetryAfter sleep then retry."""
    last = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn()
        except TelegramRetryAfter as e:
            last = e
            if attempt == max_retries:
                raise
            logger.warning(f"Flood control: sleeping {e.retry_after}s before retry")
            await asyncio.sleep(e.retry_after)
    if last:
        raise last


async def with_send_limits(chat_id: int, chat_type: str | None, coro_fn, max_retries=2):
    await SEND_LIMITER.wait(chat_id=chat_id, chat_type=chat_type)
    return await with_flood_retry(coro_fn, max_retries=max_retries)


# --- Helper Function for Routing ---

def _source_id_from_messages(messages: list[types.Message]) -> int:
    """Source identifier for logging: chat id for channels, from_user.id otherwise."""
    m = messages[0]
    if m.chat.type in ("channel", "supergroup", "group"):
        return m.chat.id
    return m.from_user.id if m.from_user else 0


async def route_content(
    bot: Bot,
    messages: list[types.Message],
    rule: RoutingRule,
    source_lang: str,
    max_message_length: int = 4096,
    excluded_terms: list[str] | None = None,
) -> dict:
    """
    Routes a single message or album to a specific destination based on the rule.
    Handles Translation vs Copy/Forward logic.
    """
    dest_id = rule.destination_group_id
    target_lang = rule.destination_language.lower()
    source_id = _source_id_from_messages(messages)
    logger.info(
        "action=route_content source_id=%s dest_id=%s rule_id=%s target_lang=%s",
        source_id, dest_id, getattr(rule, "id", None), target_lang,
    )
    # Identify key message (for caption/text)
    caption_msg = next((m for m in messages if m.caption), messages[0])
    
    raw_text = caption_msg.text or caption_msg.caption or ""
    entities = caption_msg.entities or caption_msg.caption_entities or []
    
    # Build HTML from Telegram text + entities (aiogram)
    from aiogram.utils.formatting import Text
    html_string = Text.from_entities(text=raw_text, entities=entities).as_html()

    # Retry loop for migration handling
    for attempt in range(2):
        try:
            link = "N/A"
            caption_too_long = False
            dest_id_str = str(dest_id).replace('-100', '')
            
            is_poll = len(messages) == 1 and getattr(messages[0], "poll", None) is not None
            translate_poll = getattr(rule, "translate_poll", False)
            if target_lang == 'none' or (is_poll and not translate_poll):
                # --- COPY MODE (no translation: explicit "none", or poll with translate_poll off — forward original so votes count together) ---
                try:
                    if len(messages) > 1:
                        # Attempt to use copyMessages (Best for albums)
                        msg_ids = [m.message_id for m in messages]
                        source_chat_id = messages[0].chat.id
                        # NOTE: Returns list of MessageId
                        await with_send_limits(dest_id, "group", lambda: bot.copy_messages(chat_id=dest_id, from_chat_id=source_chat_id, message_ids=msg_ids))
                        link = "Album Copied"
                    else:
                        # Single message
                        # Returns MessageId object (not Message)
                        sent = await with_send_limits(dest_id, "group", lambda: messages[0].copy_to(chat_id=dest_id))
                        link = f"https://t.me/c/{dest_id_str}/{sent.message_id}"
                        
                except Exception as e:
                    logger.warning(f"Copy failed (trying fallback): {e}")
                    # Fallback logic
                    if len(messages) == 1:
                         raise e
                    # Fallback for albums: One-by-one copy
                    for m in messages:
                        await with_send_limits(dest_id, "group", lambda m=m: m.copy_to(chat_id=dest_id))
                    link = "Album Copied (Separate)"

            else:
                # --- TRANSLATE / SEND MODE ---
                if is_poll and translate_poll:
                    # New poll with translated question/options (independent from original; votes don't sync)
                    msg = messages[0]
                    poll = msg.poll
                    question = await TranslationService.translate_text(poll.question, target_lang, excluded_terms=excluded_terms)
                    question = (question or poll.question)[:300]
                    options = []
                    for opt in poll.options:
                        t = await TranslationService.translate_text(opt.text, target_lang, excluded_terms=excluded_terms)
                        options.append((t or opt.text)[:100])
                    kwargs = {
                        "chat_id": dest_id,
                        "question": question,
                        "options": options,
                        "is_anonymous": poll.is_anonymous,
                        "type": poll.type,
                    }
                    if getattr(poll, "allows_multiple_answers", None) is not None:
                        kwargs["allows_multiple_answers"] = poll.allows_multiple_answers
                    if poll.type == "quiz" and getattr(poll, "correct_option_id", None) is not None:
                        kwargs["correct_option_id"] = poll.correct_option_id
                    if getattr(poll, "explanation", None):
                        expl = await TranslationService.translate_text(poll.explanation, target_lang, excluded_terms=excluded_terms)
                        kwargs["explanation"] = ((expl or poll.explanation) or "")[:200]
                    sent = await with_send_limits(dest_id, "group", lambda: bot.send_poll(**kwargs))
                    link = f"https://t.me/c/{dest_id_str}/{sent.message_id}"
                    logger.info(
                        "action=route_content result=Success source_id=%s dest_id=%s rule_id=%s link=%s (translated poll)",
                        source_id, dest_id, getattr(rule, "id", None), link,
                    )
                    return {'dest_id': dest_id, 'status': 'Success', 'link': link}

                # --- HTML pipeline for non-poll ---
                # If translated text <= 1024 and we have media: send as caption. Else: media without caption + text separately; report note.
                translated_html = html_string
                if html_string and source_lang != target_lang:
                    translated_html = await TranslationService.translate_html(html_string, target_lang, excluded_terms=excluded_terms)

                caption_ok = translated_html and len(translated_html) <= 1024
                has_media = len(messages) > 1 or (len(messages) == 1 and not getattr(messages[0], "text", None))
                caption_for_media = translated_html if (caption_ok and has_media) else None
                caption_too_long = has_media and not caption_ok

                from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
                sent_msg = None
                translate_images = getattr(rule, "translate_images", False)

                if len(messages) > 1:
                    media_group = []
                    for msg in messages:
                        cap = caption_for_media if (msg.message_id == caption_msg.message_id) else None
                        if msg.photo and translate_images:
                            translated_bytes = await TranslationService.translate_image_native(
                                bot, msg.photo[-1].file_id, target_lang, excluded_terms=excluded_terms
                            )
                            if translated_bytes is not None:
                                media_group.append(InputMediaPhoto(
                                    media=BufferedInputFile(translated_bytes, filename="image.png"),
                                    caption=cap,
                                    parse_mode="HTML",
                                ))
                            else:
                                media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=cap, parse_mode="HTML"))
                        elif msg.photo:
                            media_group.append(InputMediaPhoto(media=msg.photo[-1].file_id, caption=cap, parse_mode="HTML"))
                        elif msg.video:
                            media_group.append(InputMediaVideo(media=msg.video.file_id, caption=cap, parse_mode="HTML"))
                        elif msg.audio:
                            media_group.append(InputMediaAudio(media=msg.audio.file_id, caption=cap, parse_mode="HTML"))
                        elif msg.document:
                            media_group.append(InputMediaDocument(media=msg.document.file_id, caption=cap, parse_mode="HTML"))

                    if media_group:
                        sent_msgs = await with_send_limits(dest_id, "group", lambda: bot.send_media_group(chat_id=dest_id, media=media_group))
                        sent_msg = sent_msgs[0]
                else:
                    msg = messages[0]
                    if msg.text:
                        pass  # translated_html sent below in chunk loop (when not caption_ok or no media)
                    elif msg.photo:
                        if translate_images:
                            translated_bytes = await TranslationService.translate_image_native(
                                bot, msg.photo[-1].file_id, target_lang, excluded_terms=excluded_terms
                            )
                            if translated_bytes is not None:
                                sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_photo(
                                    chat_id=dest_id,
                                    photo=BufferedInputFile(translated_bytes, filename="image.png"),
                                    caption=caption_for_media,
                                    parse_mode="HTML",
                                ))
                            else:
                                sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_photo(
                                    chat_id=dest_id,
                                    photo=msg.photo[-1].file_id,
                                    caption=caption_for_media,
                                    parse_mode="HTML",
                                ))
                        else:
                            sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_photo(chat_id=dest_id, photo=msg.photo[-1].file_id, caption=caption_for_media, parse_mode="HTML"))
                    elif msg.video:
                        sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_video(chat_id=dest_id, video=msg.video.file_id, caption=caption_for_media, parse_mode="HTML"))
                    elif msg.document:
                        sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_document(chat_id=dest_id, document=msg.document.file_id, caption=caption_for_media, parse_mode="HTML"))
                    elif msg.audio:
                        sent_msg = await with_send_limits(dest_id, "group", lambda: bot.send_audio(chat_id=dest_id, audio=msg.audio.file_id, caption=caption_for_media, parse_mode="HTML"))
                    elif msg.poll:
                        sent = await with_send_limits(dest_id, "group", lambda: msg.copy_to(chat_id=dest_id))
                        link = f"https://t.me/c/{dest_id_str}/{sent.message_id}"
                        sent_msg = None

                # Send translated text as separate message(s) only when not used as caption (chunk at 4096)
                if translated_html and not caption_for_media:
                    chunk_size = 4096
                    for i in range(0, len(translated_html), chunk_size):
                        chunk = translated_html[i : i + chunk_size]
                        m = await with_send_limits(dest_id, "group", lambda c=chunk: bot.send_message(chat_id=dest_id, text=c, parse_mode="HTML", disable_web_page_preview=True))
                        if sent_msg is None:
                            sent_msg = m
                
                # Link generation for Sent Messages (Message object)
                if sent_msg:
                    sent_chat = sent_msg.chat
                    if sent_chat.username:
                        link = f"https://t.me/{sent_chat.username}/{sent_msg.message_id}"
                    else:
                        link = f"https://t.me/c/{dest_id_str}/{sent_msg.message_id}"

            logger.info(
                "action=route_content result=Success source_id=%s dest_id=%s rule_id=%s link=%s",
                source_id, dest_id, getattr(rule, "id", None), link,
            )
            out = {
                'dest_id': dest_id,
                'status': 'Success',
                'link': link
            }
            if caption_too_long:
                out['caption_too_long'] = True
            return out

        except TelegramMigrateToChat as e:
            new_id = e.migrate_to_chat_id
            logger.info(f"Caught migration: {dest_id} -> {new_id}")
            async for session in get_db():
                await ChatService.handle_migration(session, dest_id, new_id)
            dest_id = new_id
            continue
            
        except Exception as e:
            logger.error(
                "action=route_content result=Failed source_id=%s dest_id=%s rule_id=%s error=%s",
                source_id, dest_id, getattr(rule, "id", None), str(e),
            )
            return {
                'dest_id': dest_id,
                'status': 'Failed',
                'error': str(e)
            }
            
    return {'dest_id': dest_id, 'status': 'Failed', 'error': 'Max retries'}


# --- Album / Group Logic ---

from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAudio, InputMediaDocument
album_cache: dict[str, list[types.Message]] = {}


async def _process_source_batch(batch: SourceMessageBatch, bot: Bot):
    source_id = batch.source_id
    source_name = batch.source_name
    source_link = batch.source_link
    messages = batch.messages
    results = []
    async for session in get_db():
        rules = await get_routing_rules_for_session(session, source_id)
        if not rules:
            return
        sys_settings = await get_system_settings(session)
        max_message_length = sys_settings.max_message_length
        cents_per_text = int(
            getattr(sys_settings, "cents_per_text_translation", None) or CENTS_PER_TEXT_DEFAULT
        )
        cents_per_image = int(
            getattr(sys_settings, "cents_per_image_translation", None) or CENTS_PER_IMAGE_DEFAULT
        )
        rules = await apply_spam_protection_and_record(session, source_id, rules, bot, source_name)
        if not rules:
            await session.commit()
            break
        batch_id = await _live_batch_create(source_id, source_name, source_link, rules)
        results, owners_set = await process_rules_for_messages(
            session,
            bot,
            rules,
            messages,
            source_id,
            source_link,
            max_message_length,
            batch_id=batch_id,
            cents_per_text=cents_per_text,
            cents_per_image=cents_per_image,
        )
        await session.commit()
        break
    if results:
        await ReportService.send_summary_report(
            bot,
            source_id,
            source_name,
            results,
            source_link,
            recipient_owner_ids=owners_set,
        )


async def _source_batch_consumer(bot: Bot):
    while True:
        batch = await _source_message_queue.get()
        try:
            await _process_source_batch(batch, bot)
        except Exception as e:
            logger.exception("source_batch_consumer failed: %s", e)
        finally:
            _source_message_queue.task_done()


def _ensure_source_consumer(bot: Bot):
    global _source_consumer_started
    if _source_consumer_started:
        return
    _source_consumer_started = True
    asyncio.create_task(_source_batch_consumer(bot))

async def process_album(media_group_id: str, bot: Bot):
    await asyncio.sleep(1.0)
    
    if media_group_id not in album_cache:
        return
    
    messages = album_cache.pop(media_group_id)
    messages.sort(key=lambda m: m.message_id)
    
    first_msg = messages[0]
    
    # Identify Source
    source_id = first_msg.chat.id if first_msg.chat.type in ['channel'] else first_msg.from_user.id
    
    # Auth
    if first_msg.chat.type == 'private':
        if not await is_authorized_user(source_id):
            return

    source_name = f"ID: {source_id}"
    source_link = None
    if first_msg.chat.type in ['channel', 'supergroup', 'group']:
        source_name = first_msg.chat.title or source_name
        if first_msg.chat.username:
            source_link = f"https://t.me/{first_msg.chat.username}/{first_msg.message_id}"
        else:
            cid = str(first_msg.chat.id).replace("-100", "")
            source_link = f"https://t.me/c/{cid}/{first_msg.message_id}"
    else:
        source_name = first_msg.from_user.full_name or source_name

    _ensure_source_consumer(bot)
    await _source_message_queue.put(
        SourceMessageBatch(
            source_id=source_id,
            source_name=source_name,
            source_link=source_link,
            messages=messages,
        )
    )


@router.message(F.migrate_to_chat_id)
async def handle_migration_event(message: types.Message):
    old_id = message.chat.id
    new_id = message.migrate_to_chat_id
    
    logger.info(f"Detected group migration from {old_id} to {new_id}")
    async for session in get_db():
        await ChatService.handle_migration(session, old_id, new_id)
    logger.info(f"Migration completed.")


@router.channel_post()
@router.message()
async def handle_message(message: types.Message, bot: Bot):
    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    # Album Check
    if message.media_group_id:
        if message.media_group_id not in album_cache:
            album_cache[message.media_group_id] = []
        album_cache[message.media_group_id].append(message)
        
        if len(album_cache[message.media_group_id]) == 1:
            asyncio.create_task(process_album(message.media_group_id, bot))
        return

    # Single Message
    source_id = message.chat.id if message.chat.type in ['channel'] else message.from_user.id
    
    if message.chat.type == 'private':
        if not await is_authorized_user(source_id):
            return
    
    source_name = f"ID: {source_id}"
    source_link = None
    if message.chat.type in ['channel', 'supergroup', 'group']:
        source_name = message.chat.title or source_name
        if message.chat.username:
            source_link = f"https://t.me/{message.chat.username}/{message.message_id}"
        else:
            cid = str(message.chat.id).replace("-100", "")
            source_link = f"https://t.me/c/{cid}/{message.message_id}"
    else:
        source_name = message.from_user.full_name or source_name

    _ensure_source_consumer(bot)
    await _source_message_queue.put(
        SourceMessageBatch(
            source_id=source_id,
            source_name=source_name,
            source_link=source_link,
            messages=[message],
        )
    )


router.message.middleware(BlockedUserMiddleware())
router.callback_query.middleware(BlockedUserMiddleware())
